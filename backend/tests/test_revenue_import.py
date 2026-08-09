"""CSV revenue import tests.

Real payout statements are messy, so these exercise the awkward cases rather
than a tidy fixture: currency symbols, thousands separators, European decimal
commas, parenthesised negatives, and headers that no exact-match list would
cover. The rule throughout is that an unreadable row is reported, never
silently imported as zero.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import Provenance
from app.models.revenue import RevenueEntry, RevenueSourceType
from app.models.user import User
from app.models.x_account import XAccount
from app.services.revenue_import import (
    RevenueImporter,
    classify_source,
    detect_columns,
    parse_amount_to_minor,
    parse_date,
)


async def make_account(db: AsyncSession) -> XAccount:
    user = User(email=f"r{uuid.uuid4().hex[:8]}@example.com", password_hash="x", display_name="R")
    db.add(user)
    await db.flush()
    account = XAccount(
        user_id=user.id, x_user_id="999", username="rev", connected_at=datetime.now(UTC)
    )
    db.add(account)
    await db.flush()
    return account


class TestAmountParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("$1,234.56", 123456),
            ("1234.56", 123456),
            ("890.00", 89000),
            ("£45", 4500),
            ("1,000", 100000),
            # European convention: dot as thousands, comma as decimal.
            ("2.500,75", 250075),
            ("1234,56", 123456),
            # Accounting notation for a negative.
            ("(45.00)", -4500),
            ("-12.5", -1250),
        ],
    )
    def test_parses_real_world_formats(self, raw: str, expected: int) -> None:
        assert parse_amount_to_minor(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "abc", "N/A", "—"])
    def test_unreadable_returns_none_not_zero(self, raw: str) -> None:
        """A row we cannot read must be reported, not imported as zero."""
        assert parse_amount_to_minor(raw) is None

    def test_result_is_always_an_integer(self) -> None:
        """Money is never a float in this project."""
        assert isinstance(parse_amount_to_minor("10.99"), int)


class TestDateParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2026-07-15", date(2026, 7, 15)),
            ("15/07/2026", date(2026, 7, 15)),
            ("2026/07/15", date(2026, 7, 15)),
            ("Jul 15, 2026", date(2026, 7, 15)),
            ("15 Jul 2026", date(2026, 7, 15)),
            ("2026-07-15T09:30:00Z", date(2026, 7, 15)),
        ],
    )
    def test_parses_common_formats(self, raw: str, expected: date) -> None:
        assert parse_date(raw) == expected

    def test_unparseable_returns_none(self) -> None:
        assert parse_date("not-a-date") is None
        assert parse_date("") is None


class TestColumnDetection:
    def test_exact_headers(self) -> None:
        mapping = detect_columns(["date", "amount", "currency"])
        assert mapping["earned_at"] == "date"
        assert mapping["amount"] == "amount"

    def test_multi_word_headers(self) -> None:
        """Regression: 'Gross Amount' is a very common payout header.

        Exact matching missed it, so the whole file failed to import with a
        'could not find an amount column' error.
        """
        mapping = detect_columns(["Date", "Gross Amount", "Currency", "Source", "Memo"])
        assert mapping["amount"] == "Gross Amount"
        assert mapping["earned_at"] == "Date"

    def test_one_header_is_never_claimed_by_two_fields(self) -> None:
        """Regression: "Payout Date" was being read as the amount column.

        "payout" is a legitimate alias for an amount, so a token match grabbed
        the date column for `amount` — meaning a date would have been parsed as
        money. Each header may now be claimed once only.
        """
        mapping = detect_columns(["Payout Date", "Net Total"])
        assert mapping["earned_at"] == "Payout Date"
        assert mapping["amount"] == "Net Total"
        assert len(set(mapping.values())) == len(mapping)

    def test_missing_columns_are_simply_absent(self) -> None:
        mapping = detect_columns(["something", "unrelated"])
        assert "amount" not in mapping


class TestSourceClassification:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Ads Revenue Share", RevenueSourceType.X_ADS_SHARE),
            ("subscription", RevenueSourceType.X_SUBSCRIPTIONS),
            ("Tips", RevenueSourceType.X_TIPS),
            ("sponsorship", RevenueSourceType.SPONSORSHIP),
            ("affiliate commission", RevenueSourceType.AFFILIATE),
            ("X_ADS_SHARE", RevenueSourceType.X_ADS_SHARE),
        ],
    )
    def test_classifies_from_keywords(self, raw: str, expected: RevenueSourceType) -> None:
        assert classify_source(raw, RevenueSourceType.OTHER) is expected

    def test_unknown_falls_back_to_default(self) -> None:
        assert classify_source("mystery income", RevenueSourceType.OTHER) is RevenueSourceType.OTHER


MESSY_CSV = """Date,Gross Amount,Currency,Source,Memo
2026-07-15,"$1,234.56",USD,Ads Revenue Share,July payout
15/07/2026,"890.00",USD,sponsorship,Brand X
2026-07-20,(45.00),USD,affiliate,refund
2026-07-22,,USD,tips,missing amount
not-a-date,100.00,USD,tips,bad date
2026-07-25,0.00,USD,tips,zero row
"""


class TestImport:
    async def test_imports_good_rows_and_reports_bad_ones(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)
        result = await RevenueImporter(db_session).import_csv(account.id, MESSY_CSV)

        assert result.imported == 3
        assert len(result.errors) == 3
        reasons = " ".join(e.reason for e in result.errors)
        assert "amount" in reasons
        assert "date" in reasons
        assert "zero" in reasons

    async def test_reimport_is_a_no_op(self, db_session: AsyncSession) -> None:
        """The failure this prevents is doubling your reported revenue."""
        account = await make_account(db_session)
        importer = RevenueImporter(db_session)

        first = await importer.import_csv(account.id, MESSY_CSV)
        count_after_first = await db_session.scalar(select(func.count()).select_from(RevenueEntry))

        second = await importer.import_csv(account.id, MESSY_CSV)
        count_after_second = await db_session.scalar(select(func.count()).select_from(RevenueEntry))

        assert first.imported == 3
        assert second.imported == 0
        assert second.duplicates == 3
        assert count_after_first == count_after_second

    async def test_negative_amounts_are_preserved(self, db_session: AsyncSession) -> None:
        """Refunds are real; silently dropping them would overstate revenue."""
        account = await make_account(db_session)
        await RevenueImporter(db_session).import_csv(account.id, MESSY_CSV)

        refund = await db_session.scalar(select(RevenueEntry).where(RevenueEntry.amount_minor < 0))
        assert refund is not None
        assert refund.amount_minor == -4500

    async def test_provenance_is_imported(self, db_session: AsyncSession) -> None:
        """Never MEASURED — X has no earnings API."""
        account = await make_account(db_session)
        await RevenueImporter(db_session).import_csv(account.id, MESSY_CSV)

        entries = list(await db_session.scalars(select(RevenueEntry)))
        assert entries
        assert all(e.provenance is Provenance.IMPORTED for e in entries)

    async def test_source_is_classified_per_row(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)
        await RevenueImporter(db_session).import_csv(account.id, MESSY_CSV)

        sources = {e.source_type for e in await db_session.scalars(select(RevenueEntry))}
        assert RevenueSourceType.X_ADS_SHARE in sources
        assert RevenueSourceType.SPONSORSHIP in sources

    async def test_missing_amount_column_fails_with_a_useful_message(
        self, db_session: AsyncSession
    ) -> None:
        account = await make_account(db_session)
        result = await RevenueImporter(db_session).import_csv(account.id, "foo,bar\n1,2\n")
        assert result.imported == 0
        # The message must name the missing field and the headers actually
        # present, so the user can supply a mapping.
        assert "Could not find" in result.errors[0].reason
        assert "foo" in result.errors[0].reason

    async def test_empty_file_is_handled(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)
        result = await RevenueImporter(db_session).import_csv(account.id, "")
        assert result.imported == 0
        assert result.errors
