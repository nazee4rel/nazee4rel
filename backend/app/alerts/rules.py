"""Alert detectors: pure functions over already-computed analytics.

Two rules govern everything here.

**No model.** Alerts fire unattended, at night, into an inbox. They must not
depend on an API key being valid, a schema being honoured, or a language model
being right. Everything below is arithmetic on the analytics engine's output,
which is why an account with no Anthropic key still gets alerted when its
collection stops.

**Refuse before crying wolf.** Every statistical rule checks that its baseline
is reliable first. A follower "spike" measured against four days of history is
not a spike, it is a small number, and firing on it teaches the account owner to
ignore the channel — after which the collection-stopped alert goes unread too.
That is the failure this module is designed around, so most of the code here is
about *not* firing.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.analytics import baselines
from app.models.alerting import AlertRuleKey, AlertSeverity
from app.models.enums import Provenance
from app.services.analytics_service import (
    DashboardSummary,
    FollowerSeries,
    PostPerformance,
)

# A breakout has to clear the account's own trailing distribution by this much.
# Percentile alone is not enough: in a set of ten posts the best one is always
# at p95, and alerting on "your best post this week" every week is noise.
BREAKOUT_PERCENTILE = 90.0
BREAKOUT_MIN_POSTS = 10
BREAKOUT_MIN_RATIO = 2.0

# Sustained decline, not a bad Tuesday.
DECLINE_MIN_RATIO = 0.25
# Month-over-month revenue movement worth mentioning.
REVENUE_CHANGE_RATIO = 0.30
# Collection is hourly; two missed hours is a restart, six is a problem.
STALL_WARNING_HOURS = 6.0
STALL_CRITICAL_HOURS = 24.0
# A daily net loss this many robust sigma below the usual is worth a look.
CHURN_Z = 4.0


@dataclass(frozen=True)
class Finding:
    """One detected condition, ready to become an alert."""

    key: AlertRuleKey
    severity: AlertSeverity
    title: str
    body: str
    facts: dict[str, Any] = field(default_factory=dict)
    # Identity of the *event*. The same condition seen on ten consecutive runs
    # must produce the same key, or the dashboard fills with duplicates.
    dedupe_key: str = ""
    provenance: Provenance = Provenance.DERIVED


@dataclass
class AlertContext:
    """Everything the detectors are allowed to look at.

    Assembled once per evaluation so ten rules do not issue ten sets of
    queries, and so every rule sees the same instant.
    """

    username: str
    now: datetime
    summary: DashboardSummary
    series: FollowerSeries
    performances: list[PostPerformance]
    hours_since_snapshot: float | None
    posts_awaiting_freeze: int
    budget_state: str
    budget_fraction: float
    unavailable_capabilities: list[str]
    refresh_failure_count: int
    revenue_growth_ratio: float | None = None
    revenue_currency: str = "USD"
    revenue_latest_month: str | None = None


@dataclass(frozen=True)
class RuleSpec:
    """Static description of a rule, used for defaults and for the UI."""

    key: AlertRuleKey
    title: str
    # What it detects, and — as importantly — what it does not.
    description: str
    default_cooldown_hours: int
    # Stateful rules describe a condition that ends and can therefore resolve
    # themselves. Point-in-time rules describe a moment and never resolve.
    is_stateful: bool
    detector: Callable[[AlertContext], Finding | None]


# --------------------------------------------------------------- statistical


def detect_follower_spike(ctx: AlertContext) -> Finding | None:
    anomaly = ctx.summary.latest_anomaly
    if anomaly is None or not anomaly.is_anomalous or anomaly.direction.value != "SPIKE":
        return None
    if not anomaly.baseline.is_reliable:
        return None

    day = _latest_observed_day(ctx.series)
    return Finding(
        key=AlertRuleKey.FOLLOWER_SPIKE,
        severity=AlertSeverity.INFO,
        title=_change_headline(anomaly.value, anomaly.baseline.median, rising=True),
        body=(
            f"{anomaly.explanation} The usual daily change is "
            f"{anomaly.baseline.median:+.0f}, measured over "
            f"{anomaly.baseline.observations} days.\n\n"
            f"Which post caused it is a separate question, and one this system can "
            f"only estimate — see the Audience page for the attribution model and its "
            f"confidence intervals."
        ),
        facts={
            "daily_change": round(anomaly.value, 1),
            "usual_daily_change": round(anomaly.baseline.median, 1),
            "robust_sigma": round(anomaly.z_score, 2) if anomaly.z_score is not None else None,
            "baseline_days": anomaly.baseline.observations,
        },
        dedupe_key=f"{AlertRuleKey.FOLLOWER_SPIKE.value}:{day}",
    )


def detect_follower_drop(ctx: AlertContext) -> Finding | None:
    anomaly = ctx.summary.latest_anomaly
    if anomaly is None or not anomaly.is_anomalous or anomaly.direction.value != "DROP":
        return None
    if not anomaly.baseline.is_reliable:
        return None

    day = _latest_observed_day(ctx.series)
    severe = anomaly.severity.value in ("STRONG", "EXTREME")
    severity = AlertSeverity.WARNING if severe else AlertSeverity.INFO
    return Finding(
        key=AlertRuleKey.FOLLOWER_DROP,
        severity=severity,
        title=_change_headline(anomaly.value, anomaly.baseline.median, rising=False),
        body=(
            f"{anomaly.explanation} The usual daily change is "
            f"{anomaly.baseline.median:+.0f}.\n\n"
            f"A single day below baseline is common. This one is far enough outside "
            f"the usual spread to be worth a glance, not a reaction."
        ),
        facts={
            "daily_change": round(anomaly.value, 1),
            "usual_daily_change": round(anomaly.baseline.median, 1),
            "robust_sigma": round(anomaly.z_score, 2) if anomaly.z_score is not None else None,
        },
        dedupe_key=f"{AlertRuleKey.FOLLOWER_DROP.value}:{day}",
    )


def detect_breakout_post(ctx: AlertContext) -> Finding | None:
    """A post genuinely outside this account's own distribution.

    Two gates rather than one. Percentile alone always has a winner — in ten
    posts the best is at p95 by construction — so a breakout must also clear a
    multiple of the account's median. Otherwise this fires every week on
    whichever post happened to come first.
    """
    rated = [p for p in ctx.performances if p.engagement_rate.value is not None]
    if len(rated) < BREAKOUT_MIN_POSTS:
        return None

    rates = sorted(p.engagement_rate.value or 0.0 for p in rated)
    median = statistics.median(rates)
    if median <= 0:
        return None

    best = max(rated, key=lambda p: p.engagement_rate.value or 0.0)
    best_rate = best.engagement_rate.value or 0.0
    ratio = best_rate / median
    if best.percentile is None or best.percentile < BREAKOUT_PERCENTILE:
        return None
    if ratio < BREAKOUT_MIN_RATIO:
        return None

    return Finding(
        key=AlertRuleKey.BREAKOUT_POST,
        severity=AlertSeverity.INFO,
        title=f"A post is outperforming: {ratio:.1f}× your median rate",
        body=(
            f"“{best.text[:140]}”\n\n"
            f"Engagement rate {best_rate * 100:.2f}% against a median of "
            f"{median * 100:.2f}% across {len(rated)} comparable posts. "
            f"{best.engagement_rate.describe()}"
        ),
        facts={
            "post_id": best.post_id,
            "engagement_rate": round(best_rate, 5),
            "median_engagement_rate": round(median, 5),
            "ratio_to_median": round(ratio, 2),
            "comparable_posts": len(rated),
        },
        # Per post, so a post that keeps climbing does not re-alert daily.
        dedupe_key=f"{AlertRuleKey.BREAKOUT_POST.value}:{best.post_id}",
    )


def detect_engagement_decline(ctx: AlertContext) -> Finding | None:
    """A sustained fall in typical engagement, compared on medians.

    Medians rather than totals: one viral post can hold a total up while every
    ordinary post is doing worse, which is exactly the situation worth knowing
    about.
    """
    rated = [
        (p.posted_at, p.engagement_rate.value)
        for p in ctx.performances
        if p.engagement_rate.value is not None
    ]
    if len(rated) < 12:
        return None

    rated.sort(key=lambda item: item[0])
    midpoint = len(rated) // 2
    prior = [value for _when, value in rated[:midpoint] if value is not None]
    recent = [value for _when, value in rated[midpoint:] if value is not None]

    trend = baselines.compare_periods(recent, prior)
    if not trend.is_reliable or trend.direction != "falling":
        return None
    if trend.change_ratio is None or abs(trend.change_ratio) < DECLINE_MIN_RATIO:
        return None

    return Finding(
        key=AlertRuleKey.ENGAGEMENT_DECLINE,
        severity=AlertSeverity.WARNING,
        title=f"Typical engagement is down {abs(trend.change_ratio) * 100:.0f}%",
        body=(
            f"{trend.explanation}\n\n"
            f"Compared on medians across {len(rated)} posts, so this is not one bad "
            f"post pulling an average down — the typical post is doing worse."
        ),
        facts={
            "recent_median_rate": round(trend.recent_median, 5),
            "prior_median_rate": round(trend.prior_median, 5),
            "change_ratio": round(trend.change_ratio, 3),
            "posts_compared": len(rated),
        },
        # One per calendar week: a decline persists, and re-alerting daily
        # about the same slump is the definition of noise.
        dedupe_key=f"{AlertRuleKey.ENGAGEMENT_DECLINE.value}:{ctx.now.strftime('%G-W%V')}",
    )


def detect_revenue_change(ctx: AlertContext) -> Finding | None:
    if ctx.revenue_growth_ratio is None or ctx.revenue_latest_month is None:
        return None
    if abs(ctx.revenue_growth_ratio) < REVENUE_CHANGE_RATIO:
        return None

    direction = "up" if ctx.revenue_growth_ratio > 0 else "down"
    return Finding(
        key=AlertRuleKey.REVENUE_CHANGE,
        severity=AlertSeverity.INFO,
        title=(
            f"Recorded revenue is {direction} "
            f"{abs(ctx.revenue_growth_ratio) * 100:.0f}% month over month"
        ),
        body=(
            f"Comparing {ctx.revenue_latest_month} against the month before, in "
            f"{ctx.revenue_currency}.\n\n"
            f"This is computed from figures you entered or imported. X publishes no "
            f"creator-earnings API, so a change here can equally mean your earnings "
            f"moved or that you have not finished importing the month."
        ),
        facts={
            "month": ctx.revenue_latest_month,
            "change_ratio": round(ctx.revenue_growth_ratio, 3),
            "currency": ctx.revenue_currency,
        },
        dedupe_key=f"{AlertRuleKey.REVENUE_CHANGE.value}:{ctx.revenue_latest_month}",
        # Never DERIVED from measured data: the inputs came from a human.
        provenance=Provenance.USER_ENTERED,
    )


def detect_unusual_churn(ctx: AlertContext) -> Finding | None:
    """Net follower loss well outside the account's usual spread.

    This is the closest thing to "suspicious activity" that can be detected
    honestly from the data X exposes, and the alert body says so. A platform
    bot purge, a post that aged badly, and a compromised account all look
    identical from here — a number going down. What this rule provides is the
    timing, not the cause.
    """
    observed = [d.delta for d in ctx.series.daily if d.delta is not None]
    if len(observed) < baselines.MIN_OBSERVATIONS + 1:
        return None

    baseline = baselines.compute_baseline(observed[:-1])
    latest = observed[-1]
    if latest >= 0:
        return None

    # Reusing `detect_anomaly` rather than hand-rolling the z-score, so a
    # perfectly flat history is handled the same way here as it is for the
    # spike and drop rules — it reports a departure with no scale to quantify
    # it, rather than silently declining to fire.
    anomaly = baselines.detect_anomaly(latest, baseline, moderate=CHURN_Z)
    if anomaly.direction is not baselines.AnomalyDirection.DROP:
        return None

    scale = (
        f"That is {abs(anomaly.z_score):.1f} robust sigma below the usual daily change of "
        f"{baseline.median:+.0f}."
        if anomaly.z_score is not None
        else f"The usual daily change is {baseline.median:+.0f}, and the history has been "
        f"flat enough that there is no spread to measure this against."
    )

    day = _latest_observed_day(ctx.series)
    return Finding(
        key=AlertRuleKey.UNUSUAL_CHURN,
        severity=AlertSeverity.WARNING,
        title=f"Net follower loss of {abs(latest):.0f} in a day",
        body=(
            f"{scale}\n\n"
            f"What this does not tell you is why. A platform bot purge, a post that "
            f"aged badly and a compromised account all look the same from the outside: "
            f"a number going down. X exposes no follower-event stream, so this alert "
            f"gives you the timing and nothing more. Check the posts around this date "
            f"before concluding anything."
        ),
        facts={
            "net_change": round(latest, 1),
            "usual_daily_change": round(baseline.median, 1),
            "robust_sigma": round(anomaly.z_score, 2) if anomaly.z_score is not None else None,
        },
        dedupe_key=f"{AlertRuleKey.UNUSUAL_CHURN.value}:{day}",
    )


# ---------------------------------------------------------------- operational


def detect_collection_stalled(ctx: AlertContext) -> Finding | None:
    """The most important rule in this file.

    Every other alert describes something that already happened and can be read
    about later. This one describes data being lost right now: impressions
    inside the 30-day window cannot be re-fetched at any price, so an hour of
    stopped collection is an hour that is gone.
    """
    hours = ctx.hours_since_snapshot
    if hours is None:
        # Never collected at all. Real, but it is a setup problem rather than a
        # failure, and it would otherwise fire forever on a fresh install.
        return None
    if hours < STALL_WARNING_HOURS:
        return None

    severity = AlertSeverity.CRITICAL if hours >= STALL_CRITICAL_HOURS else AlertSeverity.WARNING
    return Finding(
        key=AlertRuleKey.COLLECTION_STALLED,
        severity=severity,
        title=f"Collection has not run for {hours:.0f} hours",
        body=(
            f"The most recent follower snapshot is {hours:.1f} hours old, against an "
            f"hourly schedule.\n\n"
            f"This is worth acting on quickly. X returns impressions only for posts "
            f"under 30 days old and publishes no follower history at all, so every "
            f"hour of stopped collection is permanently missing from the record — "
            f"there is no backfill for it. Check the worker and beat containers."
        ),
        facts={"hours_since_snapshot": round(hours, 1)},
        # No date in the key: this is one ongoing condition, not a daily event.
        dedupe_key=AlertRuleKey.COLLECTION_STALLED.value,
    )


def detect_freeze_at_risk(ctx: AlertContext) -> Finding | None:
    if ctx.posts_awaiting_freeze <= 0:
        return None
    return Finding(
        key=AlertRuleKey.FREEZE_AT_RISK,
        severity=AlertSeverity.WARNING,
        title=f"{ctx.posts_awaiting_freeze} post(s) approach the 30-day metrics cliff",
        body=(
            f"{ctx.posts_awaiting_freeze} post(s) are inside the final day before X stops "
            f"returning their impressions, and no final snapshot has been taken yet.\n\n"
            f"The collector has a dedicated hourly sweep for exactly this, so it will "
            f"usually clear on its own. If it does not, those impressions become "
            f"unavailable permanently rather than temporarily."
        ),
        facts={"posts_awaiting_freeze": ctx.posts_awaiting_freeze},
        dedupe_key=AlertRuleKey.FREEZE_AT_RISK.value,
    )


def detect_budget_pressure(ctx: AlertContext) -> Finding | None:
    if ctx.budget_state in ("healthy", "warning"):
        return None
    severity = AlertSeverity.CRITICAL if ctx.budget_state == "exhausted" else AlertSeverity.WARNING
    return Finding(
        key=AlertRuleKey.BUDGET_PRESSURE,
        severity=severity,
        title=f"X API budget is {ctx.budget_state} ({ctx.budget_fraction * 100:.0f}% used)",
        body=(
            "The cost governor has stepped collection down to protect the ceiling. "
            "Snapshot resolution drops first; the pre-cliff freeze is never skipped, "
            "because that data cannot be re-acquired.\n\n"
            "Raise X_MONTHLY_BUDGET_USD or wait for the next billing period."
        ),
        facts={
            "budget_state": ctx.budget_state,
            "fraction_used": round(ctx.budget_fraction, 3),
        },
        dedupe_key=AlertRuleKey.BUDGET_PRESSURE.value,
    )


def detect_access_degraded(ctx: AlertContext) -> Finding | None:
    """Token refresh failures or capabilities that have stopped working.

    Grouped because they have the same consequence — data stops arriving — and
    the same fix path: reconnect the account.
    """
    if ctx.refresh_failure_count == 0 and not ctx.unavailable_capabilities:
        return None

    parts: list[str] = []
    if ctx.refresh_failure_count:
        parts.append(
            f"The stored X token has failed to refresh {ctx.refresh_failure_count} time(s). "
            f"X rotates refresh tokens on every exchange and invalidates the old one, so a "
            f"repeated failure usually means the credential is spent and the account needs "
            f"reconnecting."
        )
    if ctx.unavailable_capabilities:
        parts.append(
            "These capabilities are not available at the current access level, so anything "
            "depending on them is absent rather than zero: "
            + ", ".join(sorted(ctx.unavailable_capabilities))
            + "."
        )

    severity = AlertSeverity.CRITICAL if ctx.refresh_failure_count >= 3 else AlertSeverity.WARNING
    return Finding(
        key=AlertRuleKey.ACCESS_DEGRADED,
        severity=severity,
        title="X API access is degraded",
        body="\n\n".join(parts),
        facts={
            "refresh_failures": ctx.refresh_failure_count,
            "unavailable_capabilities": sorted(ctx.unavailable_capabilities),
        },
        dedupe_key=AlertRuleKey.ACCESS_DEGRADED.value,
    )


# ------------------------------------------------------------------ registry

REGISTRY: dict[AlertRuleKey, RuleSpec] = {
    spec.key: spec
    for spec in (
        RuleSpec(
            key=AlertRuleKey.COLLECTION_STALLED,
            title="Collection has stopped",
            description=(
                "Fires when the hourly follower snapshot has not run for several hours. "
                "The only alert here about data being lost as you read it — impressions "
                "inside the 30-day window cannot be re-fetched afterwards."
            ),
            default_cooldown_hours=6,
            is_stateful=True,
            detector=detect_collection_stalled,
        ),
        RuleSpec(
            key=AlertRuleKey.FREEZE_AT_RISK,
            title="Posts near the 30-day cliff",
            description=(
                "Posts within a day of losing their impressions permanently, with no "
                "final snapshot taken. Usually clears itself via the freeze sweep."
            ),
            default_cooldown_hours=12,
            is_stateful=True,
            detector=detect_freeze_at_risk,
        ),
        RuleSpec(
            key=AlertRuleKey.ACCESS_DEGRADED,
            title="X API access degraded",
            description=(
                "Token refresh failures or capabilities that have stopped working. Does "
                "not detect account compromise — only that this application's access has "
                "broken."
            ),
            default_cooldown_hours=12,
            is_stateful=True,
            detector=detect_access_degraded,
        ),
        RuleSpec(
            key=AlertRuleKey.BUDGET_PRESSURE,
            title="API budget under pressure",
            description=(
                "The cost governor has begun degrading collection to stay inside the "
                "monthly ceiling."
            ),
            default_cooldown_hours=24,
            is_stateful=True,
            detector=detect_budget_pressure,
        ),
        RuleSpec(
            key=AlertRuleKey.FOLLOWER_SPIKE,
            title="Unusual follower growth",
            description=(
                "A daily change well outside the account's own usual spread, measured "
                "with median and MAD so one viral day does not redefine normal."
            ),
            default_cooldown_hours=24,
            is_stateful=False,
            detector=detect_follower_spike,
        ),
        RuleSpec(
            key=AlertRuleKey.FOLLOWER_DROP,
            title="Follower growth fell sharply",
            description="The same test as the spike rule, in the other direction.",
            default_cooldown_hours=24,
            is_stateful=False,
            detector=detect_follower_drop,
        ),
        RuleSpec(
            key=AlertRuleKey.UNUSUAL_CHURN,
            title="Unusual follower loss",
            description=(
                "Net loss far outside the usual spread. Gives you the timing of an "
                "event, never its cause — X exposes no follower-event stream, so a bot "
                "purge and a bad post are indistinguishable from here."
            ),
            default_cooldown_hours=24,
            is_stateful=False,
            detector=detect_unusual_churn,
        ),
        RuleSpec(
            key=AlertRuleKey.BREAKOUT_POST,
            title="A post is outperforming",
            description=(
                "A post above the 90th percentile of the account's own trailing "
                "distribution *and* at least double its median rate. Both gates are "
                "needed: percentile alone always has a winner."
            ),
            default_cooldown_hours=12,
            is_stateful=False,
            detector=detect_breakout_post,
        ),
        RuleSpec(
            key=AlertRuleKey.ENGAGEMENT_DECLINE,
            title="Typical engagement is falling",
            description=(
                "A sustained fall in median engagement rate between the first and second "
                "halves of the window. Medians, so one viral post cannot mask it."
            ),
            default_cooldown_hours=168,
            is_stateful=False,
            detector=detect_engagement_decline,
        ),
        RuleSpec(
            key=AlertRuleKey.REVENUE_CHANGE,
            title="Recorded revenue moved",
            description=(
                "A month-over-month change in the revenue you entered or imported. Never "
                "measured — X publishes no creator-earnings API."
            ),
            default_cooldown_hours=168,
            is_stateful=False,
            detector=detect_revenue_change,
        ),
    )
}

STATEFUL_RULES = frozenset(key for key, spec in REGISTRY.items() if spec.is_stateful)


def evaluate_all(ctx: AlertContext, enabled: set[AlertRuleKey] | None = None) -> list[Finding]:
    """Run every enabled detector. One rule failing must not stop the rest."""
    findings: list[Finding] = []
    for key, spec in REGISTRY.items():
        if enabled is not None and key not in enabled:
            continue
        finding = spec.detector(ctx)
        if finding is not None:
            findings.append(finding)
    return findings


def _change_headline(value: float, usual: float, *, rising: bool) -> str:
    """Phrase a follower change so the words and the number agree.

    These rules compare against the account's own baseline, so the anomalous
    value can have either sign: growth of +5 against a usual +38 is a genuine
    fall in the growth *rate* while still being a gain in followers. Writing
    "growth fell sharply: +5" is technically true and reads as a mistake, so the
    headline is chosen from the sign of the value rather than from the direction
    of the anomaly.
    """
    if rising:
        if value >= 0:
            return f"Unusual follower growth: {value:+.0f} in a day"
        return f"Followers still fell, but by less than usual: {value:+.0f} in a day"
    if value < 0:
        return f"Followers fell by {abs(value):.0f} in a day"
    return f"Follower growth slowed to {value:+.0f} in a day, from a usual {usual:+.0f}"


def _latest_observed_day(series: FollowerSeries) -> str:
    for point in reversed(series.daily):
        if point.observed:
            return point.day
    return datetime.now(UTC).strftime("%Y-%m-%d")
