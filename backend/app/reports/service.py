"""Generating, storing and delivering reports.

Generation and delivery are separate on purpose. A report is written to the
database first and only then handed to the channels, so an unconfigured or
failing mail server produces a report you can still read in the dashboard —
with a delivery row explaining what happened to the email.

Re-running a period amends the stored report rather than creating a second one.
Running Monday's weekly job twice should not produce two Mondays.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.alerts.channels import Message, build_channels
from app.core.logging import get_logger
from app.models.alerting import (
    Delivery,
    DeliveryStatus,
    Report,
    ReportPeriod,
    ReportStatus,
)
from app.models.x_account import XAccount
from app.reports.builder import BuiltReport, ReportBuilder

log = get_logger(__name__)


def render_text(report: BuiltReport | Report) -> str:
    """Plain text, for email and for anything that wants the whole thing at once."""
    if isinstance(report, Report):
        title = report.title
        summary = report.summary
        sections = []
        for stored in report.sections:
            # Round-tripped through JSON, so nothing here is statically typed.
            raw_caveats = stored.get("caveats")
            caveats = [str(c) for c in raw_caveats] if isinstance(raw_caveats, list) else []
            sections.append(
                (
                    str(stored.get("title", "")),
                    str(stored.get("body", "")),
                    str(stored.get("provenance", "")),
                    caveats,
                )
            )
    else:
        title = report.title
        summary = report.summary
        sections = [(s.title, s.body, s.provenance, s.caveats) for s in report.sections]

    lines = [title, "=" * len(title), "", summary, ""]
    for section_title, body, provenance, caveats in sections:
        lines.append(f"## {section_title}  [{provenance}]")
        lines.append("")
        lines.append(body)
        for caveat in caveats:
            lines.append(f"  · {caveat}")
        lines.append("")
    return "\n".join(lines)


class ReportService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def generate(
        self,
        account: XAccount,
        period: ReportPeriod,
        *,
        end: date | None = None,
        deliver: bool = True,
        channels: list[str] | None = None,
    ) -> Report:
        built = await ReportBuilder(self.db).build(account, period, end=end)

        existing = await self.db.scalar(
            select(Report).where(
                Report.x_account_id == account.id,
                Report.period == period,
                Report.period_start == built.period_start,
            )
        )
        if existing is not None:
            # Amend rather than duplicate: running the job twice for the same
            # period is a retry, not a second report.
            existing.title = built.title
            existing.summary = built.summary
            existing.sections = built.as_sections()
            existing.generated_at = datetime.now(UTC)
            existing.period_end = built.period_end
            report = existing
        else:
            report = Report(
                x_account_id=account.id,
                period=period,
                period_start=built.period_start,
                period_end=built.period_end,
                generated_at=datetime.now(UTC),
                title=built.title,
                summary=built.summary,
                sections=built.as_sections(),
                status=ReportStatus.GENERATED,
            )
            self.db.add(report)
        await self.db.flush()

        if deliver:
            await self._deliver(report, built, channels)
        return report

    async def _deliver(
        self, report: Report, built: BuiltReport, channels: list[str] | None
    ) -> None:
        message = Message(subject=built.title, text=render_text(built))
        any_sent = False
        any_failed = False

        for channel in build_channels(channels or ["DASHBOARD", "EMAIL"]):
            outcome = await channel.send(message)
            self.db.add(
                Delivery(
                    report_id=report.id,
                    channel=channel.channel,
                    status=outcome.status,
                    target=outcome.target,
                    attempts=1,
                    sent_at=datetime.now(UTC) if outcome.status is DeliveryStatus.SENT else None,
                    error=outcome.error,
                )
            )
            any_sent = any_sent or outcome.status is DeliveryStatus.SENT
            any_failed = any_failed or outcome.status is DeliveryStatus.FAILED

        # A failed email does not unmake the report; it changes its delivery
        # status only, and the report stays readable.
        report.status = (
            ReportStatus.DELIVERY_FAILED
            if any_failed and not any_sent
            else ReportStatus.DELIVERED
            if any_sent
            else ReportStatus.GENERATED
        )
        await self.db.flush()
        log.info(
            "reports.delivered",
            report=str(report.id),
            period=report.period.value,
            status=report.status.value,
        )
