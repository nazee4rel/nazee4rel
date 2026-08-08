"""Loads collected history and runs the analytics engine over it.

The pure functions in `app.analytics` do the thinking; this module only feeds
them. Keeping the split means the statistics stay testable without a database,
and the database work stays free of judgement calls.

Everything returned carries enough context for the caller to know what it is
looking at — sample sizes, provenance, and why something is unavailable — so the
API layer never has to guess whether a number is trustworthy.
"""

from __future__ import annotations

import statistics
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import attribution, baselines, metrics, patterns
from app.analytics import revenue as revenue_analytics
from app.core.logging import get_logger
from app.models.content import AccountMetricSnapshot, Post, PostMetricSnapshot
from app.models.enums import Provenance
from app.models.revenue import Campaign, PostTopic, RevenueEntry, Topic
from app.models.user import User
from app.models.x_account import XAccount

log = get_logger(__name__)

# A break longer than this in the hourly series is a collection gap rather than
# a late job, and the chart should lift its pen across it.
SERIES_GAP_HOURS = 3.0

# Below this, a topic's median is one or two posts wearing a label.
MIN_POSTS_PER_TOPIC = 3


def _aware(value: datetime) -> datetime:
    """SQLite returns naive datetimes; Postgres returns aware ones."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass
class PostPerformance:
    post_id: str
    x_post_id: str
    text: str
    posted_at: datetime
    engagement: int
    weighted_engagement: float
    impressions: int | None
    engagement_rate: metrics.EngagementRate
    percentile: float | None
    has_media: bool
    has_link: bool
    is_thread: bool
    char_count: int
    post_type: str
    impressions_available: bool
    unavailable_reason: str | None = None


@dataclass
class GrowthAnalysis:
    current_followers: int | None
    daily_deltas: list[tuple[str, float]] = field(default_factory=list)
    baseline: baselines.Baseline | None = None
    latest_anomaly: baselines.Anomaly | None = None
    trend: baselines.TrendResult | None = None
    hours_of_history: int = 0
    caveats: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SeriesPoint:
    at: datetime
    followers: int


@dataclass(frozen=True)
class Gap:
    """A break in hourly collection. Charted as a discontinuity, never bridged."""

    start: datetime
    end: datetime
    hours: float


@dataclass(frozen=True)
class DailyPoint:
    day: str  # YYYY-MM-DD
    followers: int | None
    delta: float | None
    posts: int
    # False when no snapshot exists for this day. The distinction between "no
    # growth" and "no observation" is the whole point of this field.
    observed: bool


@dataclass
class FollowerSeries:
    points: list[SeriesPoint] = field(default_factory=list)
    daily: list[DailyPoint] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    provenance: Provenance = Provenance.MEASURED
    caveats: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TopicPerformance:
    topic: str
    posts: int
    median_engagement_rate: float | None
    share_of_classified: float
    is_reliable: bool

    @property
    def status(self) -> str:
        if self.is_reliable:
            return f"{self.posts} classified posts"
        return f"only {self.posts} post(s) — not enough to rank this topic"


@dataclass
class TopicAnalysis:
    topics: list[TopicPerformance] = field(default_factory=list)
    total_posts: int = 0
    classified_posts: int = 0
    unclassified_posts: int = 0
    best: str | None = None
    worst: str | None = None
    is_reliable: bool = False
    # Topic labels are model output, unlike formats, which are mechanical.
    provenance: Provenance = Provenance.INFERRED
    caveats: list[str] = field(default_factory=list)


@dataclass
class SeasonalityAnalysis:
    follower_profile: baselines.SeasonalProfile
    engagement_profile: baselines.SeasonalProfile
    follower_days_observed: int = 0
    posts_observed: int = 0
    caveats: list[str] = field(default_factory=list)


@dataclass
class DashboardSummary:
    """The Overview headline row, assembled from one consistent read."""

    followers: int | None
    hours_of_history: int
    posts: int
    window_days: int
    trend: baselines.TrendResult | None = None
    latest_anomaly: baselines.Anomaly | None = None
    growth_score: dict[str, Any] = field(default_factory=dict)

    median_daily_change: float | None = None
    baseline_reliable: bool = False
    followers_change_7d: int | None = None
    followers_change_note: str | None = None

    engagement_rate_median: float | None = None
    engagement_rate_basis: str = "NONE"
    engagement_rate_sample: int = 0

    # None, never 0: an impression total over a window where impressions were
    # never collected is not zero impressions.
    impressions_total: int | None = None
    impressions_posts: int = 0
    impressions_posts_missing: int = 0

    revenue_total_minor: int = 0
    revenue_entries: int = 0
    revenue_currency: str = "USD"

    top_post: PostPerformance | None = None
    caveats: list[str] = field(default_factory=list)


class AnalyticsService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------- loading
    async def _latest_snapshots(
        self, x_account_id: uuid.UUID, since: datetime
    ) -> dict[uuid.UUID, PostMetricSnapshot]:
        """Most recent snapshot per post.

        For posts past the 30-day window this is the frozen row — the only
        record of their impressions that will ever exist.
        """
        rows = list(
            await self.db.scalars(
                select(PostMetricSnapshot)
                .join(Post, Post.id == PostMetricSnapshot.post_id)
                .where(Post.x_account_id == x_account_id, Post.posted_at >= since)
                .order_by(PostMetricSnapshot.post_id, PostMetricSnapshot.captured_at.desc())
            )
        )
        latest: dict[uuid.UUID, PostMetricSnapshot] = {}
        for row in rows:
            latest.setdefault(row.post_id, row)
        return latest

    async def _current_followers(self, x_account_id: uuid.UUID) -> int | None:
        value = await self.db.scalar(
            select(AccountMetricSnapshot.followers_count)
            .where(AccountMetricSnapshot.x_account_id == x_account_id)
            .order_by(AccountMetricSnapshot.captured_at.desc())
            .limit(1)
        )
        return int(value) if value is not None else None

    # --------------------------------------------------------- post analysis
    async def post_performance(
        self, x_account_id: uuid.UUID, days: int = 30
    ) -> list[PostPerformance]:
        since = datetime.now(UTC) - timedelta(days=days)
        posts = list(
            await self.db.scalars(
                select(Post)
                .where(Post.x_account_id == x_account_id, Post.posted_at >= since)
                .order_by(Post.posted_at.desc())
            )
        )
        if not posts:
            return []

        latest = await self._latest_snapshots(x_account_id, since)
        followers = await self._current_followers(x_account_id)

        results: list[PostPerformance] = []
        for post in posts:
            snapshot = latest.get(post.id)
            if snapshot is None:
                continue

            engagement = metrics.total_engagement(
                snapshot.like_count,
                snapshot.reply_count,
                snapshot.retweet_count,
                snapshot.quote_count,
                snapshot.bookmark_count,
            )
            weighted = metrics.weighted_engagement(
                snapshot.like_count,
                snapshot.reply_count,
                snapshot.retweet_count,
                snapshot.quote_count,
                snapshot.bookmark_count,
            )
            impressions = snapshot.impression_count or snapshot.public_impression_count

            reason = None
            if impressions is None:
                reason = (
                    "Impressions were never collected — this post was already past "
                    "X's 30-day window when tracking began."
                    if post.discovered_after_window_closed
                    else "Impressions are not available at your current X API access level."
                )

            results.append(
                PostPerformance(
                    post_id=str(post.id),
                    x_post_id=post.x_post_id,
                    text=post.text[:280],
                    posted_at=post.posted_at,
                    engagement=engagement,
                    weighted_engagement=weighted,
                    impressions=impressions,
                    engagement_rate=metrics.engagement_rate(
                        engagement, impressions=impressions, followers=followers
                    ),
                    percentile=None,  # filled in below, once the population exists
                    has_media=post.has_media,
                    has_link=post.has_link,
                    is_thread=post.is_thread,
                    char_count=post.char_count,
                    post_type=post.post_type.value,
                    impressions_available=impressions is not None,
                    unavailable_reason=reason,
                )
            )

        # Percentiles are computed only across posts with a comparable rate, so
        # a post with no impressions does not drag the distribution.
        population = [
            r.engagement_rate.value for r in results if r.engagement_rate.value is not None
        ]
        for result in results:
            if result.engagement_rate.value is not None:
                result.percentile = metrics.percentile_rank(
                    result.engagement_rate.value, population
                )

        return results

    async def top_and_bottom(
        self, x_account_id: uuid.UUID, days: int = 30, limit: int = 5
    ) -> dict[str, Any]:
        performances = await self.post_performance(x_account_id, days)
        ranked = [p for p in performances if p.engagement_rate.value is not None]
        ranked.sort(key=lambda p: p.engagement_rate.value or 0, reverse=True)

        return {
            "top": ranked[:limit],
            "bottom": ranked[-limit:][::-1] if len(ranked) > limit else [],
            "total_posts": len(performances),
            "comparable_posts": len(ranked),
            "excluded_no_impressions": len(performances) - len(ranked),
        }

    # ------------------------------------------------------ follower growth
    async def growth(self, x_account_id: uuid.UUID, days: int = 30) -> GrowthAnalysis:
        since = datetime.now(UTC) - timedelta(days=days)
        snapshots = list(
            await self.db.scalars(
                select(AccountMetricSnapshot)
                .where(
                    AccountMetricSnapshot.x_account_id == x_account_id,
                    AccountMetricSnapshot.captured_at >= since,
                )
                .order_by(AccountMetricSnapshot.captured_at)
            )
        )

        analysis = GrowthAnalysis(
            current_followers=snapshots[-1].followers_count if snapshots else None,
            hours_of_history=len(snapshots),
        )

        if len(snapshots) < 2:
            analysis.caveats.append(
                "Follower history has only just started accumulating. X provides no "
                "history endpoint, so this series begins when collection did and "
                "cannot be backfilled."
            )
            return analysis

        # Daily aggregation for the chart; the attribution model uses hourly.
        by_day: dict[str, list[int]] = {}
        for snapshot in snapshots:
            by_day.setdefault(snapshot.captured_at.strftime("%Y-%m-%d"), []).append(
                snapshot.followers_count
            )

        days_sorted = sorted(by_day)
        deltas: list[tuple[str, float]] = []
        for previous, current in zip(days_sorted, days_sorted[1:], strict=False):
            deltas.append((current, float(max(by_day[current]) - max(by_day[previous]))))
        analysis.daily_deltas = deltas

        values = [delta for _day, delta in deltas]
        if values:
            analysis.baseline = baselines.compute_baseline(values)
            analysis.latest_anomaly = baselines.detect_anomaly(values[-1], analysis.baseline)
            midpoint = len(values) // 2
            if midpoint >= 3:
                analysis.trend = baselines.compare_periods(values[midpoint:], values[:midpoint])

        return analysis

    # -------------------------------------------------------- attribution
    async def follower_attribution(
        self, x_account_id: uuid.UUID, days: int = 30
    ) -> attribution.AttributionReport:
        """Which posts plausibly drove follower growth. Always INFERRED."""
        since = datetime.now(UTC) - timedelta(days=days)

        snapshots = [
            (
                row.captured_at if row.captured_at.tzinfo else row.captured_at.replace(tzinfo=UTC),
                row.followers_count,
            )
            for row in await self.db.scalars(
                select(AccountMetricSnapshot)
                .where(
                    AccountMetricSnapshot.x_account_id == x_account_id,
                    AccountMetricSnapshot.captured_at >= since,
                )
                .order_by(AccountMetricSnapshot.captured_at)
            )
        ]
        posts = [
            (
                str(row.id),
                row.posted_at if row.posted_at.tzinfo else row.posted_at.replace(tzinfo=UTC),
            )
            for row in await self.db.scalars(
                select(Post)
                .where(Post.x_account_id == x_account_id, Post.posted_at >= since)
                .order_by(Post.posted_at)
            )
        ]

        return attribution.attribute_followers(snapshots, posts)

    # ------------------------------------------------------------ patterns
    async def timing(self, x_account_id: uuid.UUID, days: int = 90) -> patterns.TimingAnalysis:
        # Fetched with an explicit join rather than via `account.user`, which
        # would trigger a lazy load — and lazy loading inside async SQLAlchemy
        # raises MissingGreenlet rather than silently working.
        timezone_name = (
            await self.db.scalar(
                select(User.timezone)
                .join(XAccount, XAccount.user_id == User.id)
                .where(XAccount.id == x_account_id)
            )
            or "UTC"
        )

        performances = await self.post_performance(x_account_id, days)
        observations = [
            (p.posted_at, p.engagement_rate.value)
            for p in performances
            if p.engagement_rate.value is not None
        ]
        return patterns.analyse_timing(
            [(when, value) for when, value in observations if value is not None], timezone_name
        )

    async def formats(self, x_account_id: uuid.UUID, days: int = 90) -> patterns.FormatAnalysis:
        performances = await self.post_performance(x_account_id, days)
        return patterns.analyse_formats(
            [
                (
                    patterns.PostFeatures(
                        has_media=p.has_media,
                        has_link=p.has_link,
                        is_thread=p.is_thread,
                        char_count=p.char_count,
                        post_type=p.post_type,
                    ),
                    p.engagement_rate.value,
                )
                for p in performances
                if p.engagement_rate.value is not None
            ]
        )

    # ------------------------------------------------------------- revenue
    async def revenue(self, x_account_id: uuid.UUID, currency: str = "USD") -> dict[str, Any]:
        entries = list(
            await self.db.scalars(
                select(RevenueEntry)
                .where(RevenueEntry.x_account_id == x_account_id)
                .order_by(RevenueEntry.earned_at)
            )
        )
        records = [
            revenue_analytics.RevenueRecord(
                amount_minor=e.amount_minor,
                currency=e.currency,
                earned_at=e.earned_at,
                source_type=e.source_type,
                campaign_id=str(e.campaign_id) if e.campaign_id else None,
                post_id=str(e.post_id) if e.post_id else None,
                provenance=e.provenance,
            )
            for e in entries
        ]

        summary = revenue_analytics.summarise(records, currency)

        latest = await self._latest_snapshots(x_account_id, datetime.now(UTC) - timedelta(days=365))
        impressions_by_post = {
            str(post_id): (snap.impression_count or snap.public_impression_count)
            for post_id, snap in latest.items()
        }

        campaigns = list(
            await self.db.scalars(select(Campaign).where(Campaign.x_account_id == x_account_id))
        )
        contracted = {str(c.id): c.contracted_amount_minor for c in campaigns}

        return {
            "summary": summary,
            "per_post": revenue_analytics.revenue_per_post(records, impressions_by_post),
            "per_campaign": revenue_analytics.revenue_per_campaign(records, contracted),
            "provenance": Provenance.USER_ENTERED,
            "note": (
                "X publishes no API for creator earnings, so every figure here comes "
                "from your own entries or imports. Nothing is estimated on your behalf."
            ),
        }

    # -------------------------------------------------------- growth score
    async def growth_score(self, x_account_id: uuid.UUID) -> dict[str, Any]:
        """A single 0-100 headline number, with its components exposed.

        Composite scores hide their assumptions, so the components and their
        weights are returned alongside it — and the score is withheld entirely
        when too few components can be computed, rather than being quietly
        assembled from one input.
        """
        growth = await self.growth(x_account_id, days=30)
        performances = await self.post_performance(x_account_id, days=30)

        components: dict[str, float] = {}
        missing: list[str] = []

        if growth.baseline and growth.baseline.is_reliable:
            # Normalised so ~1% daily follower growth scores highly.
            followers = growth.current_followers or 1
            daily_rate = growth.baseline.median / max(followers, 1)
            components["follower_growth"] = min(100.0, max(0.0, daily_rate * 10_000))
        else:
            missing.append("follower_growth")

        rates = [p.engagement_rate.value for p in performances if p.engagement_rate.value]
        if len(rates) >= 5:
            median_rate = sorted(rates)[len(rates) // 2]
            # ~5% engagement per impression is an excellent result.
            components["engagement"] = min(100.0, median_rate * 2000)
        else:
            missing.append("engagement")

        if len(performances) >= 5:
            days_covered = 30
            components["consistency"] = min(100.0, (len(performances) / days_covered) * 100)
        else:
            missing.append("consistency")

        if len(components) < 2:
            return {
                "score": None,
                "components": components,
                "missing": missing,
                "explanation": (
                    "Not enough history to compute a growth score yet. It needs at "
                    "least two of follower growth, engagement and posting "
                    "consistency, and those require a few weeks of collection."
                ),
                "provenance": Provenance.UNAVAILABLE,
            }

        score = sum(components.values()) / len(components)
        return {
            "score": round(score, 1),
            "components": {k: round(v, 1) for k, v in components.items()},
            "missing": missing,
            "explanation": (
                f"Mean of {len(components)} available component(s). Components are "
                f"shown individually because a single composite number hides which "
                f"part is actually moving."
            ),
            "provenance": Provenance.DERIVED,
        }

    # ------------------------------------------------------- follower series
    async def follower_series(self, x_account_id: uuid.UUID, days: int = 30) -> FollowerSeries:
        """The follower history, with its holes left as holes.

        This is the series the dashboard charts, so the treatment of missing
        data matters more than usual. A day with no snapshot yields
        `followers=None` and `delta=None` rather than a zero, and the day
        *after* a gap also yields `delta=None` — its change cannot be attributed
        to one day when the intervening day was never observed. A chart fed
        zeros would show a flat, healthy line across exactly the period where
        collection was broken.
        """
        now = datetime.now(UTC)
        since = now - timedelta(days=days)

        snapshots = list(
            await self.db.scalars(
                select(AccountMetricSnapshot)
                .where(
                    AccountMetricSnapshot.x_account_id == x_account_id,
                    AccountMetricSnapshot.captured_at >= since,
                )
                .order_by(AccountMetricSnapshot.captured_at)
            )
        )

        series = FollowerSeries()
        if not snapshots:
            series.caveats.append(
                "No follower snapshots in this period. X publishes no follower-history "
                "endpoint, so this series can only start when collection did and cannot "
                "be backfilled."
            )
            return series

        series.points = [
            SeriesPoint(at=_aware(row.captured_at), followers=row.followers_count)
            for row in snapshots
        ]

        # Breaks in hourly collection, so the chart can lift its pen rather
        # than drawing a straight line through the missing hours.
        for previous, current in zip(series.points, series.points[1:], strict=False):
            hours = (current.at - previous.at).total_seconds() / 3600
            if hours > SERIES_GAP_HOURS:
                series.gaps.append(Gap(start=previous.at, end=current.at, hours=round(hours, 1)))

        by_day: dict[str, int] = {}
        for point in series.points:
            key = point.at.strftime("%Y-%m-%d")
            by_day[key] = max(by_day.get(key, 0), point.followers)

        posts_by_day: dict[str, int] = {}
        for posted_at in await self.db.scalars(
            select(Post.posted_at).where(Post.x_account_id == x_account_id, Post.posted_at >= since)
        ):
            posts_by_day[_aware(posted_at).strftime("%Y-%m-%d")] = (
                posts_by_day.get(_aware(posted_at).strftime("%Y-%m-%d"), 0) + 1
            )

        # Walk the calendar, not the data, so absent days appear as absent.
        first = series.points[0].at.date()
        last = series.points[-1].at.date()
        cursor = first
        previous_key: str | None = None
        while cursor <= last:
            key = cursor.isoformat()
            followers = by_day.get(key)
            delta: float | None = None
            if followers is not None and previous_key is not None:
                previous_value = by_day.get(previous_key)
                if previous_value is not None:
                    delta = float(followers - previous_value)
            series.daily.append(
                DailyPoint(
                    day=key,
                    followers=followers,
                    delta=delta,
                    posts=posts_by_day.get(key, 0),
                    observed=followers is not None,
                )
            )
            previous_key = key
            cursor += timedelta(days=1)

        missing = sum(1 for d in series.daily if not d.observed)
        if missing:
            series.caveats.append(
                f"{missing} day(s) in this window have no follower snapshot. They are "
                f"drawn as breaks in the line, not as zero growth — and the day after "
                f"each break has no attributable daily change."
            )
        if series.gaps:
            longest = max(series.gaps, key=lambda g: g.hours)
            series.caveats.append(
                f"The longest collection gap was {longest.hours:.0f} hours, ending "
                f"{longest.end:%Y-%m-%d %H:%M} UTC."
            )

        return series

    # ------------------------------------------------------------- topics
    async def topic_performance(self, x_account_id: uuid.UUID, days: int = 90) -> TopicAnalysis:
        """How each topic performs, over posts the agent has classified.

        Always `INFERRED`: topic labels are model output, unlike formats, which
        are detected mechanically at collection time. The share of posts that
        are still unclassified is reported alongside, because a comparison over
        a third of your posts is a different claim from one over all of them.
        """
        since = datetime.now(UTC) - timedelta(days=days)
        performances = await self.post_performance(x_account_id, days)
        rate_by_post = {
            p.post_id: p.engagement_rate.value
            for p in performances
            if p.engagement_rate.value is not None
        }

        rows = list(
            await self.db.execute(
                select(Topic.name, PostTopic.post_id)
                .join(PostTopic, PostTopic.topic_id == Topic.id)
                .join(Post, Post.id == PostTopic.post_id)
                .where(Post.x_account_id == x_account_id, Post.posted_at >= since)
            )
        )

        analysis = TopicAnalysis(total_posts=len(performances))
        by_topic: dict[str, list[float]] = {}
        classified: set[str] = set()
        for name, post_id in rows:
            classified.add(str(post_id))
            rate = rate_by_post.get(str(post_id))
            if rate is not None:
                by_topic.setdefault(name, []).append(rate)

        analysis.classified_posts = len(classified)
        analysis.unclassified_posts = len(performances) - len(classified)

        for name, rates in sorted(by_topic.items()):
            analysis.topics.append(
                TopicPerformance(
                    topic=name,
                    posts=len(rates),
                    median_engagement_rate=statistics.median(rates),
                    share_of_classified=len(rates) / max(len(classified), 1),
                    is_reliable=len(rates) >= MIN_POSTS_PER_TOPIC,
                )
            )
        analysis.topics.sort(key=lambda t: t.median_engagement_rate or 0.0, reverse=True)

        reliable = [t for t in analysis.topics if t.is_reliable]
        analysis.is_reliable = len(reliable) >= 2
        if reliable:
            analysis.best = reliable[0].topic
            analysis.worst = reliable[-1].topic

        if not rows:
            analysis.caveats.append(
                "No posts in this window have been categorised yet. Classification runs "
                "as part of the agent cycle and needs an Anthropic API key."
            )
        elif not analysis.is_reliable:
            analysis.caveats.append(
                f"Fewer than two topics have {MIN_POSTS_PER_TOPIC}+ classified posts "
                f"behind them, so topics cannot yet be ranked against each other."
            )
        if analysis.unclassified_posts:
            analysis.caveats.append(
                f"{analysis.unclassified_posts} of {len(performances)} posts in this "
                f"window carry no topic, so this compares a subset rather than everything "
                f"you posted."
            )

        return analysis

    # -------------------------------------------------------- seasonality
    async def seasonality(self, x_account_id: uuid.UUID, days: int = 90) -> SeasonalityAnalysis:
        """Weekday rhythm, for follower change and for engagement.

        Two separate profiles because they answer different questions: which
        days you gain followers, and which days your posts land. They are
        frequently not the same day, and averaging them together would hide
        that.
        """
        series = await self.follower_series(x_account_id, days)
        follower_observations = [
            (datetime.fromisoformat(point.day).replace(tzinfo=UTC), point.delta)
            for point in series.daily
            if point.delta is not None
        ]

        performances = await self.post_performance(x_account_id, days)
        engagement_observations = [
            (_aware(p.posted_at), p.engagement_rate.value)
            for p in performances
            if p.engagement_rate.value is not None
        ]

        analysis = SeasonalityAnalysis(
            follower_profile=baselines.compute_seasonality(
                [(when, value) for when, value in follower_observations if value is not None]
            ),
            engagement_profile=baselines.compute_seasonality(
                [(when, value) for when, value in engagement_observations if value is not None]
            ),
            follower_days_observed=len(follower_observations),
            posts_observed=len(engagement_observations),
        )

        if not analysis.follower_profile.is_reliable:
            analysis.caveats.append(
                f"Follower rhythm needs {baselines.MIN_PER_WEEKDAY}+ observed days for each "
                f"of at least four weekdays. That is roughly three weeks of uninterrupted "
                f"collection."
            )
        if not analysis.engagement_profile.is_reliable:
            analysis.caveats.append(
                f"Engagement rhythm needs {baselines.MIN_PER_WEEKDAY}+ posts on each of at "
                f"least four weekdays. Posting on only two or three days a week will never "
                f"satisfy this, which is a fact about your schedule rather than a fault."
            )

        return analysis

    # --------------------------------------------------- dashboard summary
    async def dashboard_summary(self, x_account_id: uuid.UUID, days: int = 30) -> DashboardSummary:
        """One call behind the Overview headline row.

        Assembled here rather than by the page making six requests, so every
        figure on that row comes from a single consistent read of the data.
        """
        growth = await self.growth(x_account_id, days=days)
        performances = await self.post_performance(x_account_id, days)
        score = await self.growth_score(x_account_id)

        summary = DashboardSummary(
            followers=growth.current_followers,
            hours_of_history=growth.hours_of_history,
            posts=len(performances),
            trend=growth.trend,
            latest_anomaly=growth.latest_anomaly,
            growth_score=score,
            window_days=days,
            caveats=list(growth.caveats),
        )

        if growth.baseline is not None:
            summary.median_daily_change = growth.baseline.median
            summary.baseline_reliable = growth.baseline.is_reliable

        recent = [delta for _day, delta in growth.daily_deltas[-7:]]
        if len(recent) >= 7:
            summary.followers_change_7d = int(sum(recent))
        elif recent:
            summary.followers_change_note = (
                f"Only {len(recent)} day(s) of history — a seven-day change needs seven."
            )

        rates = sorted(p.engagement_rate.value for p in performances if p.engagement_rate.value)
        if rates:
            summary.engagement_rate_median = rates[len(rates) // 2]
            bases = {
                p.engagement_rate.denominator.value
                for p in performances
                if p.engagement_rate.value is not None
            }
            summary.engagement_rate_basis = bases.pop() if len(bases) == 1 else "MIXED"
            summary.engagement_rate_sample = len(rates)

        with_impressions = [p for p in performances if p.impressions is not None]
        if with_impressions:
            summary.impressions_total = sum(p.impressions or 0 for p in with_impressions)
            summary.impressions_posts = len(with_impressions)
        summary.impressions_posts_missing = len(performances) - len(with_impressions)
        if summary.impressions_posts_missing:
            summary.caveats.append(
                f"{summary.impressions_posts_missing} of {len(performances)} posts have no "
                f"impression figure, so the impression total covers only part of the window. "
                f"They are excluded rather than counted as zero."
            )

        ranked = [p for p in performances if p.engagement_rate.value is not None]
        if ranked:
            summary.top_post = max(ranked, key=lambda p: p.engagement_rate.value or 0.0)

        revenue_entries = list(
            await self.db.scalars(
                select(RevenueEntry).where(RevenueEntry.x_account_id == x_account_id)
            )
        )
        summary.revenue_total_minor = sum(e.amount_minor for e in revenue_entries)
        summary.revenue_entries = len(revenue_entries)
        if revenue_entries:
            summary.revenue_currency = revenue_entries[0].currency

        return summary
