"""When to snapshot a post, and in what order.

Pure functions, deliberately free of database or network access, because this is
the logic the whole dataset's completeness depends on and it needs to be
exhaustively testable.

Two ideas drive everything here.

**Engagement velocity is front-loaded.** Most of a post's impressions arrive in
the first day, so snapshot frequency decays with age. Sampling a three-week-old
post hourly would cost money to observe almost nothing.

**The 30-day cliff is absolute.** X stops returning `non_public_metrics` once a
post passes 30 days. Whatever else the collector skips, the pre-cliff capture
must happen — so the final freeze outranks every other kind of work and is the
last thing surrendered when the budget runs short. A missed hourly snapshot
costs a little resolution; a missed freeze costs the impressions permanently.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# X's hard limit on non-public metrics.
METRICS_WINDOW_DAYS = 30

# The freeze window opens here. A full day of slack before the cliff means a
# failed run, a rate-limit pause or an outage still has retries left.
FREEZE_OPENS_AT_DAY = 29.0

# Tracking stops at the cliff. Public metrics remain readable afterwards, but
# they drift slowly and every read costs money, so continuing would buy very
# little for a recurring bill.
TRACKING_STOPS_AT_DAY = float(METRICS_WINDOW_DAYS)


class SnapshotPriority(enum.IntEnum):
    """Lower sorts first. Used to order work when budget or rate limit binds."""

    FINAL_FREEZE = 0  # irreplaceable — never yield this
    NEVER_CAPTURED = 1  # no impressions on record at all yet
    FRESH = 2  # inside the first day, where the curve is steepest
    RECENT = 3
    MATURE = 4


@dataclass(frozen=True)
class CadenceTier:
    name: str
    max_age_hours: float
    interval_hours: float
    priority: SnapshotPriority


# Roughly 71 snapshots over a post's tracked life. At 5 posts/day that is about
# 10,650 resource-reads a month — near $10.65 at the Owned Reads rate.
CADENCE_TIERS: tuple[CadenceTier, ...] = (
    CadenceTier("first_day", 24.0, 1.0, SnapshotPriority.FRESH),
    CadenceTier("first_week", 24.0 * 7, 6.0, SnapshotPriority.RECENT),
    CadenceTier("first_month", FREEZE_OPENS_AT_DAY * 24, 24.0, SnapshotPriority.MATURE),
)


class BudgetState(enum.StrEnum):
    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True)
class SnapshotDecision:
    due: bool
    reason: str
    priority: SnapshotPriority = SnapshotPriority.MATURE
    is_final_freeze: bool = False

    def __bool__(self) -> bool:
        return self.due


def age_hours(posted_at: datetime, now: datetime) -> float:
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=UTC)
    return max(0.0, (now - posted_at).total_seconds() / 3600.0)


def tier_for_age(hours: float) -> CadenceTier | None:
    for tier in CADENCE_TIERS:
        if hours < tier.max_age_hours:
            return tier
    return None


def window_closes_at(posted_at: datetime) -> datetime:
    """When X will stop returning this post's non-public metrics."""
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=UTC)
    return posted_at + timedelta(days=METRICS_WINDOW_DAYS)


def is_in_freeze_window(hours: float) -> bool:
    return FREEZE_OPENS_AT_DAY * 24 <= hours < TRACKING_STOPS_AT_DAY * 24


def decide(
    *,
    posted_at: datetime,
    last_snapshot_at: datetime | None,
    has_final_freeze: bool,
    impressions_ever_collected: bool,
    now: datetime | None = None,
    budget_state: BudgetState = BudgetState.HEALTHY,
) -> SnapshotDecision:
    """Decide whether one post is due for a snapshot right now.

    Order matters: the freeze is evaluated before the budget check, because the
    governor is allowed to reduce resolution but never to forfeit data that
    cannot be re-acquired.
    """
    now = now or datetime.now(UTC)
    hours = age_hours(posted_at, now)

    # 1. Past the cliff: nothing more can be learned that is worth paying for.
    if hours >= TRACKING_STOPS_AT_DAY * 24:
        return SnapshotDecision(
            False,
            "Past the 30-day window — X no longer returns non-public metrics for this post.",
        )

    # 2. The mandatory capture. This outranks everything, including a critical
    #    budget, because the alternative is permanent loss.
    if is_in_freeze_window(hours) and not has_final_freeze:
        return SnapshotDecision(
            True,
            "Final freeze: the metrics window closes within 24 hours.",
            SnapshotPriority.FINAL_FREEZE,
            is_final_freeze=True,
        )

    # 3. Everything else yields to an exhausted budget.
    if budget_state is BudgetState.EXHAUSTED:
        return SnapshotDecision(False, "Budget exhausted; only final freezes still run.")

    if has_final_freeze:
        return SnapshotDecision(False, "Already frozen; no further snapshots are needed.")

    tier = tier_for_age(hours)
    if tier is None:
        return SnapshotDecision(False, "No cadence tier applies.")

    # 4. Never sampled at all — take one regardless of cadence, so that a post
    #    discovered late is not left with no record whatsoever.
    if last_snapshot_at is None:
        return SnapshotDecision(
            True,
            "No snapshot recorded for this post yet.",
            SnapshotPriority.NEVER_CAPTURED if not impressions_ever_collected else tier.priority,
        )

    if last_snapshot_at.tzinfo is None:
        last_snapshot_at = last_snapshot_at.replace(tzinfo=UTC)
    since_last = (now - last_snapshot_at).total_seconds() / 3600.0

    interval = effective_interval(tier, budget_state)
    if since_last < interval:
        return SnapshotDecision(
            False,
            f"Snapshotted {since_last:.1f}h ago; the {tier.name} interval is {interval:.0f}h.",
        )

    return SnapshotDecision(
        True,
        f"Due: {since_last:.1f}h since the last snapshot ({tier.name} tier).",
        tier.priority,
    )


def effective_interval(tier: CadenceTier, budget_state: BudgetState) -> float:
    """Stretch the cadence as the budget tightens.

    The degradation ladder from the architecture doc. Note it only ever reduces
    *resolution* — the freeze path above never reaches this function, so no
    amount of budget pressure can cost you the irreplaceable capture.
    """
    if budget_state is BudgetState.HEALTHY:
        return tier.interval_hours
    if budget_state is BudgetState.WARNING:
        # Halve the sampling rate, floored at daily.
        return min(24.0, tier.interval_hours * 2)
    # CRITICAL: daily at most, whatever the tier.
    return 24.0


def expected_snapshots_per_post() -> int:
    """Rough snapshot count over a post's tracked life, for cost estimation."""
    total = 0.0
    previous_bound = 0.0
    for tier in CADENCE_TIERS:
        span = tier.max_age_hours - previous_bound
        total += span / tier.interval_hours
        previous_bound = tier.max_age_hours
    return int(total) + 1  # +1 for the final freeze


def estimate_monthly_resources(posts_per_day: float) -> int:
    """Resource-reads per month for a given posting rate.

    Drives the budget projection shown on the dashboard, so the user sees what
    their cadence costs before the invoice does.
    """
    posts_in_window = posts_per_day * METRICS_WINDOW_DAYS
    return int(posts_in_window * expected_snapshots_per_post())


def sort_by_priority(
    decisions: list[tuple[object, SnapshotDecision]],
) -> list[tuple[object, SnapshotDecision]]:
    """Order due work so the most urgent survives a truncated run."""
    return sorted(
        (d for d in decisions if d[1].due),
        key=lambda item: item[1].priority,
    )
