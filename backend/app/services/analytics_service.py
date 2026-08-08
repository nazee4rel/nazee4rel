"""Loads collected history and runs the analytics engine over it.

The pure functions in `app.analytics` do the thinking; this module only feeds
them. Keeping the split means the statistics stay testable without a database,
and the database work stays free of judgement calls.

Everything returned carries enough context for the caller to know what it is
looking at — sample sizes, provenance, and why something is unavailable — so the
API layer never has to guess whether a number is trustworthy.
"""

from __future__ import annotations

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
from app.models.revenue import Campaign, RevenueEntry
from app.models.user import User
from app.models.x_account import XAccount

log = get_logger(__name__)


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
