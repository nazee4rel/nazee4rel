"""CSV import for revenue.

X has no payouts API, so revenue arrives as statements you export yourself. The
import is built around three assumptions about real-world CSVs:

* Column names vary. Rather than demanding one layout, common aliases are
  recognised and the caller can override the mapping.
* Amounts are written inconsistently — "$1,234.56", "1234.56", "(45.00)" for a
  negative. Parsing is deliberate about this and reports rows it cannot read
  rather than silently coercing them to zero.
* Statements get re-imported. Every row gets a stable content hash used as
  `external_ref`, so importing the same file twice adds nothing instead of
  doubling your reported revenue.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.enums import Provenance
from app.models.revenue import RevenueEntry, RevenueSourceType

log = get_logger(__name__)

# Aliases seen across Stripe exports, X statements and hand-kept spreadsheets.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "earned_at": ("date", "earned_at", "payout_date", "created", "created_at", "period", "day"),
    "amount": ("amount", "gross", "net", "total", "payout", "earnings", "value"),
    "currency": ("currency", "curr", "ccy"),
    "source_type": ("source", "type", "source_type", "category", "product"),
    "description": ("description", "memo", "note", "notes", "details"),
    "external_ref": ("id", "reference", "ref", "transaction_id", "payout_id", "external_ref"),
}

SOURCE_KEYWORDS: dict[RevenueSourceType, tuple[str, ...]] = {
    RevenueSourceType.X_ADS_SHARE: ("ads", "ad revenue", "revenue share", "adshare"),
    RevenueSourceType.X_SUBSCRIPTIONS: ("subscription", "subscriber"),
    RevenueSourceType.X_TIPS: ("tip", "tips"),
    RevenueSourceType.SPONSORSHIP: ("sponsor", "sponsorship"),
    RevenueSourceType.AFFILIATE: ("affiliate", "referral", "commission"),
    RevenueSourceType.BRAND_DEAL: ("brand", "partnership", "collab"),
}

DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y", "%b %d, %Y", "%d %b %Y")


@dataclass
class RowError:
    row_number: int
    reason: str
    raw: dict[str, str] = field(default_factory=dict)


@dataclass
class ImportResult:
    batch_id: str
    imported: int = 0
    duplicates: int = 0
    errors: list[RowError] = field(default_factory=list)
    detected_columns: dict[str, str] = field(default_factory=dict)
    total_minor: int = 0

    @property
    def rows_seen(self) -> int:
        return self.imported + self.duplicates + len(self.errors)

    def summary(self) -> str:
        parts = [f"{self.imported} imported"]
        if self.duplicates:
            parts.append(f"{self.duplicates} already present")
        if self.errors:
            parts.append(f"{len(self.errors)} could not be read")
        return ", ".join(parts) + "."


def detect_columns(headers: list[str]) -> dict[str, str]:
    """Map our field names onto the file's actual headers.

    Matched in two passes: exact header name first, then per-token. The second
    pass is what handles real statement headers like "Gross Amount" or "Payout
    Date", which no exact-match list would ever cover exhaustively. Matching on
    tokens rather than substrings avoids "amount" latching onto something like
    "amount_refunded_to_customer".
    """
    normalised = {h.strip().lower().replace(" ", "_"): h for h in headers}
    mapping: dict[str, str] = {}
    # A header may only be claimed once. Without this, "Payout Date" gets taken
    # as the amount column (because "payout" is an amount alias) and a date is
    # read as money — silent corruption of exactly the kind this importer exists
    # to prevent.
    claimed: set[str] = set()

    # Exact matches across all fields first, so a precise header always wins
    # over another field's loose token match.
    for field_name, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in normalised and normalised[alias] not in claimed:
                mapping[field_name] = normalised[alias]
                claimed.add(normalised[alias])
                break

    for field_name, aliases in COLUMN_ALIASES.items():
        if field_name in mapping:
            continue
        for header, original in normalised.items():
            if original in claimed:
                continue
            if set(header.split("_")) & set(aliases):
                mapping[field_name] = original
                claimed.add(original)
                break

    return mapping


def parse_amount_to_minor(raw: str) -> int | None:
    """Parse a money string into integer minor units.

    Handles currency symbols, thousands separators, and both `-45.00` and
    `(45.00)` for negatives. Returns None on anything unparseable so the caller
    can report the row rather than importing a silent zero.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None

    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]

    cleaned = re.sub(r"[^\d.,\-]", "", text)
    if not cleaned:
        return None

    # If both separators appear, the rightmost is the decimal point.
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        # A lone comma with exactly two trailing digits is a decimal comma;
        # otherwise it is a thousands separator.
        parts = cleaned.split(",")
        cleaned = cleaned.replace(",", "." if len(parts[-1]) == 2 else "")

    try:
        value = float(cleaned)
    except ValueError:
        return None

    if negative:
        value = -abs(value)
    return int(round(value * 100))


def parse_date(raw: str) -> date | None:
    text = str(raw).strip()
    if not text:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()  # noqa: DTZ007
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def classify_source(raw: str | None, default: RevenueSourceType) -> RevenueSourceType:
    if not raw:
        return default
    text = raw.strip().lower()

    # Exact enum name wins over keyword guessing.
    for source in RevenueSourceType:
        if text == source.value.lower():
            return source

    for source, keywords in SOURCE_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return source
    return default


def row_fingerprint(x_account_id: uuid.UUID, row: dict[str, str]) -> str:
    """Stable hash of a row's content, used to make re-imports idempotent."""
    payload = "|".join(f"{k}={v}" for k, v in sorted(row.items()) if v)
    digest = hashlib.sha256(f"{x_account_id}:{payload}".encode()).hexdigest()
    return f"csv:{digest[:32]}"


class RevenueImporter:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def import_csv(
        self,
        x_account_id: uuid.UUID,
        content: str,
        *,
        default_source: RevenueSourceType = RevenueSourceType.OTHER,
        default_currency: str = "USD",
        column_overrides: dict[str, str] | None = None,
    ) -> ImportResult:
        batch_id = uuid.uuid4().hex[:16]
        result = ImportResult(batch_id=batch_id)

        reader = csv.DictReader(io.StringIO(content))
        if not reader.fieldnames:
            result.errors.append(RowError(0, "The file has no header row."))
            return result

        mapping = detect_columns(list(reader.fieldnames))
        mapping.update(column_overrides or {})
        result.detected_columns = mapping

        for required in ("earned_at", "amount"):
            if required not in mapping:
                result.errors.append(
                    RowError(
                        0,
                        f"Could not find a '{required}' column. Headers seen: "
                        f"{', '.join(reader.fieldnames)}. Supply a column mapping "
                        f"to import this file.",
                    )
                )
                return result

        for row_number, row in enumerate(reader, start=2):
            clean = {k: (v or "").strip() for k, v in row.items() if k}

            earned_at = parse_date(clean.get(mapping["earned_at"], ""))
            if earned_at is None:
                result.errors.append(RowError(row_number, "Unreadable or missing date.", clean))
                continue

            amount_minor = parse_amount_to_minor(clean.get(mapping["amount"], ""))
            if amount_minor is None:
                result.errors.append(RowError(row_number, "Unreadable or missing amount.", clean))
                continue
            if amount_minor == 0:
                # Zero rows are usually padding or subtotals rather than income.
                result.errors.append(RowError(row_number, "Amount is zero; skipped.", clean))
                continue

            external_ref = (
                clean.get(mapping["external_ref"], "") if "external_ref" in mapping else ""
            ) or row_fingerprint(x_account_id, clean)

            existing = await self.db.scalar(
                select(RevenueEntry.id).where(
                    RevenueEntry.x_account_id == x_account_id,
                    RevenueEntry.external_ref == external_ref,
                )
            )
            if existing is not None:
                result.duplicates += 1
                continue

            self.db.add(
                RevenueEntry(
                    x_account_id=x_account_id,
                    source_type=classify_source(
                        clean.get(mapping["source_type"]) if "source_type" in mapping else None,
                        default_source,
                    ),
                    amount_minor=amount_minor,
                    currency=(
                        clean.get(mapping["currency"], default_currency) or default_currency
                    ).upper()[:3]
                    if "currency" in mapping
                    else default_currency,
                    earned_at=earned_at,
                    # Imported, not measured — and certainly not from X.
                    provenance=Provenance.IMPORTED,
                    external_ref=external_ref,
                    description=(
                        clean.get(mapping["description"]) if "description" in mapping else None
                    ),
                    import_batch=batch_id,
                    details={"source_row": row_number},
                )
            )
            result.imported += 1
            result.total_minor += amount_minor

        await self.db.flush()
        log.info(
            "revenue.import_complete",
            batch=batch_id,
            imported=result.imported,
            duplicates=result.duplicates,
            errors=len(result.errors),
        )
        return result
