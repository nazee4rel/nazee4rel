"""Period reports: daily, weekly, monthly.

A report is built as structured sections rather than as rendered text, so the
same generated report can be shown in the dashboard and emailed without being
computed twice — and so a section that has nothing to say can be omitted
honestly instead of printed empty.

The through-line from every earlier phase holds here. A section states its
provenance and its sample sizes; a metric that was never collected says so
rather than printing zero; and the AI sections quote insights the agent already
grounded rather than asking a model to summarise the summary.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.agent import (
    AgentRun,
    Insight,
    Recommendation,
    RecommendationStatus,
    VerificationResult,
)
from app.models.alerting import Alert, AlertState, ReportPeriod
from app.models.enums import Provenance
from app.models.x_account import XAccount
from app.services.analytics_service import AnalyticsService

log = get_logger(__name__)

PERIOD_DAYS = {ReportPeriod.DAILY: 1, ReportPeriod.WEEKLY: 7, ReportPeriod.MONTHLY: 30}


@dataclass
class ReportSection:
    key: str
    title: str
    body: str
    facts: dict[str, Any] = field(default_factory=dict)
    provenance: str = Provenance.DERIVED.value
    caveats: list[str] = field(default_factory=list)


@dataclass
class BuiltReport:
    period: ReportPeriod
    period_start: date
    period_end: date
    title: str
    summary: str
    sections: list[ReportSection] = field(default_factory=list)

    def as_sections(self) -> list[dict[str, Any]]:
        return [asdict(section) for section in self.sections]


class ReportBuilder:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.analytics = AnalyticsService(db)

    async def build(
        self, account: XAccount, period: ReportPeriod, *, end: date | None = None
    ) -> BuiltReport:
        days = PERIOD_DAYS[period]
        period_end = end or datetime.now(UTC).date()
        period_start = period_end - timedelta(days=days - 1)

        report = BuiltReport(
            period=period,
            period_start=period_start,
            period_end=period_end,
            title=f"{period.value.capitalize()} report for @{account.username} — "
            f"{period_start:%d %b} to {period_end:%d %b %Y}",
            summary="",
        )

        summary = await self.analytics.dashboard_summary(account.id, days=max(days, 7))
        series = await self.analytics.follower_series(account.id, days=max(days, 7))

        report.sections.append(self._followers(summary, series, days))
        report.sections.append(await self._content(account, days))
        revenue_section = await self._revenue(account)
        if revenue_section is not None:
            report.sections.append(revenue_section)
        report.sections.append(await self._alerts(account, period_start))
        insights_section = await self._insights(account, period_start)
        if insights_section is not None:
            report.sections.append(insights_section)
        actions_section = await self._recommendations(account)
        if actions_section is not None:
            report.sections.append(actions_section)
        report.sections.append(await self._data_quality(account, series, summary))

        report.summary = self._headline(summary, series, days)
        log.info(
            "reports.built",
            account=account.username,
            period=period.value,
            sections=len(report.sections),
        )
        return report

    # ------------------------------------------------------------- sections
    def _followers(self, summary: Any, series: Any, days: int) -> ReportSection:
        observed = [d for d in series.daily[-days:] if d.observed and d.delta is not None]
        gained = sum(d.delta for d in observed)
        missing = sum(1 for d in series.daily[-days:] if not d.observed)

        lines = [
            f"Followers now: {summary.followers:,}"
            if summary.followers is not None
            else "Followers: not collected yet.",
        ]
        if observed:
            lines.append(f"Net change over {len(observed)} observed day(s): {gained:+.0f}.")
        if summary.baseline_reliable and summary.median_daily_change is not None:
            lines.append(
                f"Typical day: {summary.median_daily_change:+.0f} (median, so one "
                f"exceptional day does not move it)."
            )
        if summary.trend is not None and summary.trend.is_reliable:
            lines.append(summary.trend.explanation)

        caveats: list[str] = []
        if missing:
            caveats.append(
                f"{missing} day(s) in this period have no snapshot, so the net change "
                f"covers only the days actually observed — it is not a total for the period."
            )

        return ReportSection(
            key="followers",
            title="Follower growth",
            body="\n".join(lines),
            facts={
                "followers": summary.followers,
                "net_change_observed_days": round(gained, 1) if observed else None,
                "observed_days": len(observed),
                "unobserved_days": missing,
            },
            provenance=Provenance.MEASURED.value,
            caveats=caveats,
        )

    async def _content(self, account: XAccount, days: int) -> ReportSection:
        performances = await self.analytics.post_performance(account.id, days=max(days, 7))
        rated = [p for p in performances if p.engagement_rate.value is not None]
        missing = len(performances) - len(rated)

        if not performances:
            return ReportSection(
                key="content",
                title="Content",
                body="No posts in this period.",
                facts={"posts": 0},
                provenance=Provenance.MEASURED.value,
            )

        lines = [f"{len(performances)} post(s) in this period."]
        best = worst = None
        if rated:
            ordered = sorted(rated, key=lambda p: p.engagement_rate.value or 0.0)
            worst, best = ordered[0], ordered[-1]
            # "impressions unavailable" rather than "0 impressions": for a post
            # past the 30-day window the figure was never obtainable.
            impressions = (
                f"{best.impressions:,} impressions"
                if best.impressions is not None
                else "impressions unavailable"
            )
            lines.append(
                f"\nBest: “{best.text[:120]}”\n"
                f"  {best.engagement} engagements, {impressions}, "
                f"{(best.engagement_rate.value or 0) * 100:.2f}% rate."
            )
            if len(ordered) > 1:
                lines.append(
                    f"\nWeakest: “{worst.text[:120]}”\n"
                    f"  {(worst.engagement_rate.value or 0) * 100:.2f}% rate."
                )

        caveats: list[str] = []
        if missing:
            caveats.append(
                f"{missing} post(s) have no usable denominator and were excluded from the "
                f"ranking rather than ranked at zero."
            )

        return ReportSection(
            key="content",
            title="Content",
            body="\n".join(lines),
            facts={
                "posts": len(performances),
                "comparable_posts": len(rated),
                "best_post_id": best.post_id if best else None,
                "worst_post_id": worst.post_id if worst and len(rated) > 1 else None,
            },
            provenance=Provenance.DERIVED.value,
            caveats=caveats,
        )

    async def _revenue(self, account: XAccount) -> ReportSection | None:
        revenue = await self.analytics.revenue(account.id)
        summary = revenue["summary"]
        if summary.entry_count == 0:
            return None

        lines = [
            f"Total recorded: {summary.total_minor / 100:,.2f} {summary.currency} "
            f"across {summary.entry_count} entries."
        ]
        if summary.best_source is not None:
            source = summary.best_source.source_type.value.replace("_", " ").lower()
            lines.append(f"Largest source: {source}.")
        if summary.growth_ratio is not None:
            lines.append(f"Month over month: {summary.growth_ratio * 100:+.0f}%.")

        return ReportSection(
            key="revenue",
            title="Revenue",
            body="\n".join(lines),
            facts={
                "total_minor": summary.total_minor,
                "currency": summary.currency,
                "entries": summary.entry_count,
                "growth_ratio": summary.growth_ratio,
            },
            # Never measured. X publishes no creator-earnings API.
            provenance=Provenance.USER_ENTERED.value,
            caveats=[
                "Every figure here was entered or imported by you. X publishes no "
                "creator-earnings API, so nothing in this section was measured."
            ],
        )

    async def _alerts(self, account: XAccount, since: date) -> ReportSection:
        raised = list(
            await self.db.scalars(
                select(Alert)
                .where(
                    Alert.x_account_id == account.id,
                    Alert.fired_at >= datetime.combine(since, datetime.min.time(), tzinfo=UTC),
                )
                .order_by(Alert.fired_at.desc())
            )
        )
        still_open = [a for a in raised if a.state is AlertState.FIRING]

        if not raised:
            body = "No alerts were raised in this period."
        else:
            body = "\n".join(
                f"[{a.severity.value}] {a.title}"
                + ("" if a.state is AlertState.FIRING else f" ({a.state.value.lower()})")
                for a in raised[:10]
            )

        return ReportSection(
            key="alerts",
            title="Alerts",
            body=body,
            facts={"raised": len(raised), "still_open": len(still_open)},
            provenance=Provenance.DERIVED.value,
        )

    async def _insights(self, account: XAccount, since: date) -> ReportSection | None:
        insights = list(
            await self.db.scalars(
                select(Insight)
                .where(
                    Insight.x_account_id == account.id,
                    Insight.created_at >= datetime.combine(since, datetime.min.time(), tzinfo=UTC),
                )
                .order_by(Insight.created_at.desc())
                .limit(5)
            )
        )
        if not insights:
            return None

        return ReportSection(
            key="insights",
            title="What the agent concluded",
            body="\n\n".join(f"{i.title}\n{i.body}" for i in insights),
            facts={"insights": len(insights)},
            # Model interpretation of measured data, so INFERRED rather than
            # DERIVED — the same badge the dashboard shows.
            provenance=Provenance.INFERRED.value,
            caveats=[
                "Each of these cited figures from the analytics engine, and those "
                "citations were verified against the data before the insight was stored."
            ],
        )

    async def _recommendations(self, account: XAccount) -> ReportSection | None:
        open_recommendations = list(
            await self.db.scalars(
                select(Recommendation)
                .where(
                    Recommendation.x_account_id == account.id,
                    Recommendation.status == RecommendationStatus.PROPOSED,
                )
                .order_by(Recommendation.created_at.desc())
                .limit(5)
            )
        )
        graded = list(
            await self.db.scalars(
                select(Recommendation)
                .where(
                    Recommendation.x_account_id == account.id,
                    Recommendation.verification_result != VerificationResult.PENDING,
                )
                .order_by(Recommendation.verified_at.desc())
                .limit(5)
            )
        )
        if not open_recommendations and not graded:
            return None

        parts: list[str] = []
        if open_recommendations:
            parts.append(
                "Awaiting your decision:\n"
                + "\n".join(
                    f"- {r.title} (checkable after {r.verify_after})" for r in open_recommendations
                )
            )
        if graded:
            parts.append(
                "Previously predicted, now graded:\n"
                + "\n".join(f"- [{r.verification_result.value}] {r.title}" for r in graded)
            )

        return ReportSection(
            key="recommendations",
            title="Recommended actions",
            body="\n\n".join(parts),
            facts={"open": len(open_recommendations), "graded": len(graded)},
            provenance=Provenance.INFERRED.value,
        )

    async def _data_quality(self, account: XAccount, series: Any, summary: Any) -> ReportSection:
        """Stated in every report on purpose.

        A report that silently covers 60% of the period reads exactly like one
        that covers all of it. Since the missing part can never be recovered,
        the coverage belongs in the report rather than in a log.
        """
        unobserved = sum(1 for d in series.daily if not d.observed)
        lines = [
            f"Follower snapshots: {len(series.daily) - unobserved} of {len(series.daily)} "
            f"day(s) observed."
        ]
        if summary.impressions_posts_missing:
            lines.append(
                f"{summary.impressions_posts_missing} post(s) have no impression figure. "
                f"They are excluded from rates and totals rather than counted as zero."
            )
        if series.gaps:
            longest = max(series.gaps, key=lambda g: g.hours)
            lines.append(
                f"Longest collection gap: {longest.hours:.0f} hours, ending "
                f"{longest.end:%Y-%m-%d %H:%M} UTC. That window cannot be backfilled."
            )
        if not series.gaps and not unobserved and not summary.impressions_posts_missing:
            lines.append("No gaps. Everything in this report covers the full period.")

        return ReportSection(
            key="data_quality",
            title="Data coverage",
            body="\n".join(lines),
            facts={
                "days_observed": len(series.daily) - unobserved,
                "days_total": len(series.daily),
                "posts_missing_impressions": summary.impressions_posts_missing,
                "collection_gaps": len(series.gaps),
            },
            provenance=Provenance.MEASURED.value,
        )

    # -------------------------------------------------------------- headline
    def _headline(self, summary: Any, series: Any, days: int) -> str:
        observed = [d for d in series.daily[-days:] if d.observed and d.delta is not None]
        if not observed:
            return (
                "No follower observations in this period, so there is nothing to report "
                "on growth. Check that collection is running."
            )
        gained = sum(d.delta for d in observed)
        parts = [f"{gained:+.0f} followers over {len(observed)} observed day(s)"]
        if summary.engagement_rate_median is not None:
            parts.append(f"median engagement {summary.engagement_rate_median * 100:.2f}%")
        if summary.posts:
            parts.append(f"{summary.posts} post(s)")
        return ", ".join(parts) + "."


async def latest_run_id(db: AsyncSession, x_account_id: uuid.UUID) -> uuid.UUID | None:
    value = await db.scalar(
        select(AgentRun.id)
        .where(AgentRun.x_account_id == x_account_id)
        .order_by(AgentRun.started_at.desc())
        .limit(1)
    )
    return value if isinstance(value, uuid.UUID) else None
