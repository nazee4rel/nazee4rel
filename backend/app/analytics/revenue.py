"""Revenue analytics.

Two rules run through everything here.

**Money stays in integer minor units** until the moment it is displayed.
These figures are summed, split across posts and divided into impressions;
floats would accumulate error through all of it.

**RPM is suppressed rather than approximated.** Revenue per 1,000 impressions
needs both halves. Where impressions were never collected — a post already past
30 days when collection began — the metric is reported as unavailable, not
computed against a partial denominator. A confidently wrong RPM is worse than an
absent one, because it invites decisions.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from app.models.enums import Provenance
from app.models.revenue import RevenueSourceType


@dataclass(frozen=True)
class RevenueRecord:
    """Flat view of a revenue entry, for the pure functions below."""

    amount_minor: int
    currency: str
    earned_at: date
    source_type: RevenueSourceType
    campaign_id: str | None = None
    post_id: str | None = None
    provenance: Provenance = Provenance.USER_ENTERED


@dataclass
class SourceBreakdown:
    source_type: RevenueSourceType
    total_minor: int
    entry_count: int
    share: float


@dataclass
class MonthlyRevenue:
    month: str  # YYYY-MM
    total_minor: int
    entry_count: int


@dataclass
class RevenueSummary:
    currency: str
    total_minor: int
    entry_count: int
    by_source: list[SourceBreakdown] = field(default_factory=list)
    by_month: list[MonthlyRevenue] = field(default_factory=list)
    growth_ratio: float | None = None
    best_source: SourceBreakdown | None = None
    # Every figure here originates from you, never from X.
    provenance: Provenance = Provenance.USER_ENTERED
    caveats: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        return self.total_minor / 100


def _month_key(value: date) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def summarise(records: list[RevenueRecord], currency: str = "USD") -> RevenueSummary:
    """Totals by source and month, plus month-over-month growth."""
    matching = [r for r in records if r.currency == currency]
    summary = RevenueSummary(currency=currency, total_minor=0, entry_count=len(matching))

    skipped = len(records) - len(matching)
    if skipped:
        # Summing across currencies without rates would be arithmetic on
        # incomparable units.
        summary.caveats.append(
            f"{skipped} entr(ies) in other currencies were excluded. Totals cover "
            f"{currency} only — no exchange rates are applied."
        )

    if not matching:
        return summary

    summary.total_minor = sum(r.amount_minor for r in matching)

    by_source: dict[RevenueSourceType, list[RevenueRecord]] = defaultdict(list)
    for record in matching:
        by_source[record.source_type].append(record)

    summary.by_source = sorted(
        (
            SourceBreakdown(
                source_type=source,
                total_minor=sum(r.amount_minor for r in entries),
                entry_count=len(entries),
                share=(
                    sum(r.amount_minor for r in entries) / summary.total_minor
                    if summary.total_minor
                    else 0.0
                ),
            )
            for source, entries in by_source.items()
        ),
        key=lambda s: s.total_minor,
        reverse=True,
    )
    summary.best_source = summary.by_source[0] if summary.by_source else None

    by_month: dict[str, list[RevenueRecord]] = defaultdict(list)
    for record in matching:
        by_month[_month_key(record.earned_at)].append(record)

    summary.by_month = [
        MonthlyRevenue(
            month=month,
            total_minor=sum(r.amount_minor for r in entries),
            entry_count=len(entries),
        )
        for month, entries in sorted(by_month.items())
    ]

    if len(summary.by_month) >= 2:
        previous, latest = summary.by_month[-2], summary.by_month[-1]
        if previous.total_minor > 0:
            summary.growth_ratio = (
                latest.total_minor - previous.total_minor
            ) / previous.total_minor
        else:
            summary.caveats.append(
                "The previous month had no recorded revenue, so no growth ratio is meaningful."
            )

    return summary


@dataclass
class PostRevenue:
    post_id: str
    revenue_minor: int
    impressions: int | None
    rpm_minor: float | None
    rpm_available: bool
    reason_unavailable: str | None = None


def revenue_per_post(
    records: list[RevenueRecord],
    impressions_by_post: dict[str, int | None],
) -> list[PostRevenue]:
    """Revenue and RPM per post, suppressing RPM where impressions are unknown."""
    totals: dict[str, int] = defaultdict(int)
    for record in records:
        if record.post_id:
            totals[record.post_id] += record.amount_minor

    results: list[PostRevenue] = []
    for post_id, revenue_minor in totals.items():
        impressions = impressions_by_post.get(post_id)

        if impressions is None:
            results.append(
                PostRevenue(
                    post_id=post_id,
                    revenue_minor=revenue_minor,
                    impressions=None,
                    rpm_minor=None,
                    rpm_available=False,
                    reason_unavailable=(
                        "Impressions were never collected for this post — it was "
                        "already past X's 30-day window when tracking began, so "
                        "RPM cannot be computed."
                    ),
                )
            )
            continue

        if impressions == 0:
            results.append(
                PostRevenue(
                    post_id=post_id,
                    revenue_minor=revenue_minor,
                    impressions=0,
                    rpm_minor=None,
                    rpm_available=False,
                    reason_unavailable="This post recorded zero impressions, so RPM is undefined.",
                )
            )
            continue

        results.append(
            PostRevenue(
                post_id=post_id,
                revenue_minor=revenue_minor,
                impressions=impressions,
                rpm_minor=revenue_minor * 1000 / impressions,
                rpm_available=True,
            )
        )

    return sorted(results, key=lambda r: r.revenue_minor, reverse=True)


@dataclass
class CampaignPerformance:
    campaign_id: str
    revenue_minor: int
    contracted_minor: int | None
    fulfilment_ratio: float | None
    entry_count: int


def revenue_per_campaign(
    records: list[RevenueRecord], contracted: dict[str, int | None]
) -> list[CampaignPerformance]:
    """Recorded revenue against contracted value, per campaign."""
    totals: dict[str, list[RevenueRecord]] = defaultdict(list)
    for record in records:
        if record.campaign_id:
            totals[record.campaign_id].append(record)

    results = []
    for campaign_id, entries in totals.items():
        received = sum(r.amount_minor for r in entries)
        agreed = contracted.get(campaign_id)
        results.append(
            CampaignPerformance(
                campaign_id=campaign_id,
                revenue_minor=received,
                contracted_minor=agreed,
                fulfilment_ratio=(received / agreed) if agreed else None,
                entry_count=len(entries),
            )
        )

    return sorted(results, key=lambda c: c.revenue_minor, reverse=True)


def estimated_rpm_across(total_revenue_minor: int, total_impressions: int | None) -> float | None:
    """Account-level RPM.

    Uses only impressions actually collected, so the caller must pass a
    denominator built from posts with known impressions — otherwise the figure
    is inflated by revenue attributed to posts whose reach was never recorded.
    """
    if not total_impressions:
        return None
    return total_revenue_minor * 1000 / total_impressions
