"""The evidence bundle: everything the model is allowed to reason from.

The agent never hands Claude a database connection, a tool, or a question it
must go and answer. It hands it a flat dictionary of facts computed by the
deterministic analytics engine, and requires every claim to cite keys from that
dictionary. Three things follow from that design:

* **Grounding is checkable.** After the call, each cited key/value pair is
  compared against this bundle. A figure that is not here, or that is here with
  a different value, is caught rather than published.
* **Absence is visible.** Missing data appears as an explicit entry in
  `unavailable` rather than as a zero or a silently absent key, so the model can
  say "impressions were never collected for 12 of these posts" instead of
  reasoning over a hole.
* **Runs are auditable.** The bundle is stored on the run, so an insight from
  three months ago can still be checked against exactly what produced it.

Post text is included, because content advice without content is worthless. It
is fenced and labelled as data, and its presence sets
`ingested_untrusted_content` — see `app.agent.prompts` for why free text of any
origin is treated that way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import baselines
from app.collectors import schedule
from app.models.content import AccountMetricSnapshot, Post
from app.models.enums import CapabilityStatus
from app.models.revenue import PostTopic, Topic
from app.models.x_account import AccountCapability, XAccount
from app.services.analytics_service import AnalyticsService

FactValue = float | int | str | bool | None

# Post text is truncated in the prompt: the first 240 characters carry the
# format and subject, and full text would multiply the token cost of a run
# across a hundred posts for very little added signal.
POST_TEXT_LIMIT = 240

# Enough posts to see a pattern without turning the prompt into a corpus.
MAX_POSTS_IN_PROMPT = 30


def format_fact(value: FactValue) -> str:
    """Canonical string form of a fact, used in the prompt and in grounding.

    Floats are rounded on the way in so the model is never asked to echo back
    fourteen decimal places, and so a citation can be compared exactly.
    """
    if value is None:
        return "unavailable"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if value == int(value) and abs(value) < 1e15:
            return str(int(value))
        return f"{value:.4g}"
    return str(value)


@dataclass
class Evidence:
    """Facts, context and provenance for one agent run."""

    facts: dict[str, FactValue] = field(default_factory=dict)
    # Human-readable statements that are not numbers: caveats, sample-size
    # warnings, methodology notes.
    notes: list[str] = field(default_factory=list)
    # Things that are genuinely not obtainable, stated as such.
    unavailable: list[str] = field(default_factory=list)
    posts: list[dict[str, Any]] = field(default_factory=list)
    # Short reference (p1, p2 …) -> post UUID, so the model can cite a post
    # without handling UUIDs and the result can still be linked back.
    post_refs: dict[str, str] = field(default_factory=dict)
    period_start: datetime | None = None
    period_end: datetime | None = None
    ingested_untrusted_content: bool = False

    def add(self, key: str, value: FactValue) -> None:
        self.facts[key] = value

    def has_enough_to_reason(self) -> bool:
        """Whether a run is worth making at all.

        A model asked to explain four data points will produce something
        confident and useless, and it will cost money to do it.
        """
        posts = self.facts.get("content.posts_analysed")
        hours = self.facts.get("account.hours_of_history")
        return (isinstance(posts, int) and posts >= 3) or (isinstance(hours, int) and hours >= 48)

    def to_json(self) -> dict[str, Any]:
        return {
            "facts": {k: v for k, v in self.facts.items()},
            "notes": self.notes,
            "unavailable": self.unavailable,
            "posts": self.posts,
            "post_refs": self.post_refs,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "ingested_untrusted_content": self.ingested_untrusted_content,
        }

    def render_facts(self) -> str:
        return "\n".join(f"{key} = {format_fact(value)}" for key, value in self.facts.items())


class EvidenceBuilder:
    """Assembles the bundle from stored history. No model involved."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.analytics = AnalyticsService(db)

    async def build(self, account: XAccount, *, days: int = 30) -> Evidence:
        now = datetime.now(UTC)
        evidence = Evidence(period_start=now - timedelta(days=days), period_end=now)

        evidence.add("account.username", account.username)
        evidence.add("account.window_days", days)

        await self._add_collection_health(evidence, account, now)
        await self._add_growth(evidence, account, days)
        await self._add_content(evidence, account, days)
        await self._add_timing_and_formats(evidence, account)
        await self._add_attribution(evidence, account, days)
        await self._add_topics(evidence, account, days)
        await self._add_revenue(evidence, account)
        await self._add_capabilities(evidence, account)

        return evidence

    # ------------------------------------------------------------- sections
    async def _add_collection_health(
        self, evidence: Evidence, account: XAccount, now: datetime
    ) -> None:
        last = await self.db.scalar(
            select(func.max(AccountMetricSnapshot.captured_at)).where(
                AccountMetricSnapshot.x_account_id == account.id
            )
        )
        hours_since: float | None = None
        if last is not None:
            captured = last if last.tzinfo else last.replace(tzinfo=UTC)
            hours_since = round((now - captured).total_seconds() / 3600, 2)
        evidence.add("data.hours_since_last_snapshot", hours_since)

        awaiting = int(
            await self.db.scalar(
                select(func.count())
                .select_from(Post)
                .where(
                    Post.x_account_id == account.id,
                    Post.final_freeze_at.is_(None),
                    Post.metrics_window_closes_at > now,
                    Post.metrics_window_closes_at
                    <= now
                    + timedelta(days=schedule.METRICS_WINDOW_DAYS - schedule.FREEZE_OPENS_AT_DAY),
                )
            )
            or 0
        )
        never = int(
            await self.db.scalar(
                select(func.count())
                .select_from(Post)
                .where(
                    Post.x_account_id == account.id,
                    Post.discovered_after_window_closed.is_(True),
                )
            )
            or 0
        )
        evidence.add("data.posts_awaiting_final_freeze", awaiting)
        evidence.add("data.posts_impressions_never_available", never)

        if hours_since is None:
            evidence.unavailable.append(
                "No follower snapshot has ever been recorded, so there is no growth history. "
                "X publishes no follower-history endpoint; this series can only start when "
                "collection does and can never be backfilled."
            )
        elif hours_since >= 2:
            evidence.notes.append(
                f"The most recent follower snapshot is {hours_since:.1f} hours old. "
                f"Collection may have stopped."
            )
        if never:
            evidence.unavailable.append(
                f"{never} post(s) were first seen after their 30-day metrics window had "
                f"already closed. Their impressions are permanently unavailable — not "
                f"missing through a collection failure."
            )

    async def _add_growth(self, evidence: Evidence, account: XAccount, days: int) -> None:
        growth = await self.analytics.growth(account.id, days=days)
        evidence.add("account.followers_current", growth.current_followers)
        evidence.add("account.hours_of_history", growth.hours_of_history)
        evidence.add("growth.days_measured", len(growth.daily_deltas))

        if growth.baseline is not None:
            evidence.add("growth.median_daily_change", round(growth.baseline.median, 2))
            evidence.add("growth.typical_variation", round(growth.baseline.robust_sigma, 2))
            evidence.add("growth.baseline_reliable", growth.baseline.is_reliable)
            if not growth.baseline.is_reliable:
                evidence.notes.append(
                    f"The follower baseline rests on {growth.baseline.observations} day(s); "
                    f"{baselines.MIN_OBSERVATIONS} are needed before deviations mean anything."
                )
        if growth.daily_deltas:
            evidence.add("growth.latest_daily_change", round(growth.daily_deltas[-1][1], 2))
        if growth.latest_anomaly is not None:
            anomaly = growth.latest_anomaly
            evidence.add("growth.latest_direction", anomaly.direction.value)
            evidence.add("growth.latest_severity", anomaly.severity.value)
            evidence.add(
                "growth.latest_z_score",
                round(anomaly.z_score, 2) if anomaly.z_score is not None else None,
            )
        if growth.trend is not None:
            evidence.add("growth.trend", growth.trend.direction)
            evidence.add(
                "growth.trend_change_ratio",
                round(growth.trend.change_ratio, 3)
                if growth.trend.change_ratio is not None
                else None,
            )
        evidence.notes.extend(growth.caveats)

    async def _add_content(self, evidence: Evidence, account: XAccount, days: int) -> None:
        performances = await self.analytics.post_performance(account.id, days=days)
        evidence.add("content.posts_analysed", len(performances))

        with_impressions = [p for p in performances if p.impressions is not None]
        evidence.add("content.posts_with_impressions", len(with_impressions))
        missing = len(performances) - len(with_impressions)
        if missing:
            evidence.unavailable.append(
                f"{missing} of {len(performances)} posts have no impression figure. Rates for "
                f"those are computed against followers instead, or withheld — they are not "
                f"treated as zero impressions."
            )

        rates = sorted(p.engagement_rate.value for p in performances if p.engagement_rate.value)
        if rates:
            evidence.add("content.median_engagement_rate", round(rates[len(rates) // 2], 5))
            evidence.add("content.best_engagement_rate", round(rates[-1], 5))
            evidence.add("content.worst_engagement_rate", round(rates[0], 5))
        impressions = sorted(p.impressions for p in with_impressions if p.impressions is not None)
        if impressions:
            evidence.add("content.median_impressions", impressions[len(impressions) // 2])

        if performances:
            evidence.add("content.posts_per_week", round(len(performances) / max(days / 7, 1), 2))

        # Ranked so the model sees the extremes, which is where the pattern is.
        ranked = sorted(
            (p for p in performances if p.engagement_rate.value is not None),
            key=lambda p: p.engagement_rate.value or 0.0,
            reverse=True,
        )
        selected = ranked[: MAX_POSTS_IN_PROMPT // 2] + ranked[-(MAX_POSTS_IN_PROMPT // 2) :]
        seen: set[str] = set()
        for index, performance in enumerate(selected, start=1):
            if performance.post_id in seen:
                continue
            seen.add(performance.post_id)
            ref = f"p{index}"
            evidence.post_refs[ref] = performance.post_id
            evidence.posts.append(
                {
                    "ref": ref,
                    "posted_at": performance.posted_at.isoformat(),
                    "text": performance.text[:POST_TEXT_LIMIT],
                    "engagement": performance.engagement,
                    "impressions": performance.impressions,
                    "engagement_rate": round(performance.engagement_rate.value, 5)
                    if performance.engagement_rate.value is not None
                    else None,
                    "rate_basis": performance.engagement_rate.denominator.value,
                    "has_media": performance.has_media,
                    "has_link": performance.has_link,
                    "is_thread": performance.is_thread,
                    "post_type": performance.post_type,
                    "char_count": performance.char_count,
                }
            )
            evidence.add(f"post.{ref}.engagement", performance.engagement)
            evidence.add(f"post.{ref}.impressions", performance.impressions)
            evidence.add(
                f"post.{ref}.engagement_rate",
                round(performance.engagement_rate.value, 5)
                if performance.engagement_rate.value is not None
                else None,
            )

        if evidence.posts:
            # Post text is free text this system did not author. See the module
            # docstring and `prompts.UNTRUSTED_*`.
            evidence.ingested_untrusted_content = True

    async def _add_timing_and_formats(self, evidence: Evidence, account: XAccount) -> None:
        timing = await self.analytics.timing(account.id)
        evidence.add("timing.timezone", timing.timezone)
        evidence.add("timing.posts_considered", timing.total_posts)
        evidence.add("timing.is_reliable", timing.is_reliable)
        for index, bucket in enumerate(timing.best_hours[:3], start=1):
            evidence.add(f"timing.best_{index}_label", bucket.label)
            evidence.add(f"timing.best_{index}_median_rate", round(bucket.median_performance, 5))
            evidence.add(f"timing.best_{index}_posts", bucket.sample_size)
        evidence.notes.extend(timing.caveats)

        formats = await self.analytics.formats(account.id)
        evidence.add("formats.posts_considered", formats.total_posts)
        evidence.add("formats.is_reliable", formats.is_reliable)
        if formats.best is not None:
            evidence.add("formats.best_label", formats.best.label)
            evidence.add("formats.best_median_rate", round(formats.best.median_performance, 5))
            evidence.add("formats.best_posts", formats.best.sample_size)
        if formats.worst is not None:
            evidence.add("formats.worst_label", formats.worst.label)
            evidence.add("formats.worst_median_rate", round(formats.worst.median_performance, 5))
            evidence.add("formats.worst_posts", formats.worst.sample_size)
        evidence.notes.extend(formats.caveats)

    async def _add_attribution(self, evidence: Evidence, account: XAccount, days: int) -> None:
        report = await self.analytics.follower_attribution(account.id, days=days)
        evidence.add("attribution.confidence", report.confidence.value)
        evidence.add("attribution.observed_growth", round(report.total_observed_growth, 1))
        evidence.add("attribution.explained_by_posts", round(report.total_explained, 1))
        evidence.add("attribution.unexplained", round(report.unexplained, 1))
        evidence.add(
            "attribution.posts_with_clear_effect", len(report.distinguishable_contributions)
        )

        by_post_id = {v: k for k, v in evidence.post_refs.items()}
        for contribution in report.distinguishable_contributions[:5]:
            ref = by_post_id.get(contribution.post_id)
            if ref is None:
                continue
            evidence.add(f"attribution.{ref}.followers", round(contribution.estimate, 1))
            evidence.add(f"attribution.{ref}.ci_low", round(contribution.ci_low, 1))
            evidence.add(f"attribution.{ref}.ci_high", round(contribution.ci_high, 1))

        evidence.notes.extend(report.caveats)
        if not report.is_usable:
            evidence.unavailable.append(
                "Follower attribution could not be modelled: " + report.summary()
            )
        else:
            evidence.notes.append(
                "Follower attribution is INFERRED from a statistical model, never measured. "
                "X does not report which post gained which follower. Treat every attribution "
                "figure as an estimate with the stated interval."
            )

    async def _add_topics(self, evidence: Evidence, account: XAccount, days: int) -> None:
        since = datetime.now(UTC) - timedelta(days=days)
        rows = list(
            await self.db.execute(
                select(Topic.name, func.count(PostTopic.id))
                .join(PostTopic, PostTopic.topic_id == Topic.id)
                .join(Post, Post.id == PostTopic.post_id)
                .where(Post.x_account_id == account.id, Post.posted_at >= since)
                .group_by(Topic.name)
                .order_by(func.count(PostTopic.id).desc())
            )
        )
        evidence.add("topics.assigned_labels", len(rows))
        for name, count in rows[:8]:
            evidence.add(f"topic.{name}.posts", int(count))
        if not rows:
            evidence.unavailable.append(
                "No posts have been categorised yet, so topic performance cannot be compared. "
                "Categorisation runs as part of the agent cycle."
            )

    async def _add_revenue(self, evidence: Evidence, account: XAccount) -> None:
        revenue = await self.analytics.revenue(account.id)
        summary = revenue["summary"]
        evidence.add("revenue.entries_recorded", summary.entry_count)
        evidence.add("revenue.currency", summary.currency)
        evidence.add("revenue.total_minor_units", summary.total_minor)
        best = summary.best_source
        evidence.add("revenue.best_source", best.source_type.value if best else None)
        evidence.add("revenue.best_source_minor_units", best.total_minor if best else None)
        evidence.add(
            "revenue.month_over_month_ratio",
            round(summary.growth_ratio, 3) if summary.growth_ratio is not None else None,
        )
        evidence.unavailable.append(
            "X publishes no creator-earnings API. Every revenue figure above was entered or "
            "imported by the account owner. Do not estimate revenue, extrapolate it, or "
            "describe it as measured."
        )

    async def _add_capabilities(self, evidence: Evidence, account: XAccount) -> None:
        rows = list(
            await self.db.scalars(
                select(AccountCapability).where(AccountCapability.x_account_id == account.id)
            )
        )
        blocked = [r.capability.value for r in rows if r.status is not CapabilityStatus.AVAILABLE]
        evidence.add("capabilities.checked", len(rows))
        evidence.add("capabilities.unavailable_count", len(blocked))
        if blocked:
            evidence.unavailable.append(
                "These X API capabilities are not available at this access level, so anything "
                "depending on them is absent rather than zero: " + ", ".join(sorted(blocked)) + "."
            )
