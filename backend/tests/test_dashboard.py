"""Dashboard aggregate tests.

The dashboard is where a missing value is most likely to be quietly rendered as
a zero, because charts want dense arrays and tables want numbers. So most of
what is asserted here is the shape of absence: a day with no snapshot is not a
day with no growth, an impression total over posts that were never measured is
not zero impressions, and a topic with two posts behind it is not a ranking.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.content import AccountMetricSnapshot, Post, PostMetricSnapshot
from app.models.enums import Provenance
from app.models.revenue import PostTopic, RevenueEntry, RevenueSourceType, Topic
from app.models.user import User
from app.models.x_account import XAccount
from app.services.analytics_service import AnalyticsService

NOW = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)


async def make_account(db: AsyncSession) -> XAccount:
    user = User(
        email=f"d{uuid.uuid4().hex[:8]}@example.com",
        password_hash="x",
        display_name="Dash",
        timezone="UTC",
    )
    db.add(user)
    await db.flush()
    account = XAccount(
        user_id=user.id,
        x_user_id=uuid.uuid4().hex[:12],
        username="dash",
        connected_at=NOW,
    )
    db.add(account)
    await db.flush()
    return account


async def add_snapshots(
    db: AsyncSession,
    account: XAccount,
    *,
    days: int,
    skip_days: set[int] = frozenset(),  # type: ignore[assignment]
    followers_start: int = 1000,
    per_day_gain: int = 5,
) -> None:
    """Follower snapshots twice a day, optionally missing whole days.

    Anchored to midnight rather than to `NOW`, so that "skip day 3" removes one
    calendar date rather than half of two.
    """
    start = NOW.replace(hour=0) - timedelta(days=days)
    followers = followers_start
    for day in range(days + 1):
        if day in skip_days:
            followers += per_day_gain  # growth still happened; we just did not see it
            continue
        for hour in (0, 12):
            captured = start + timedelta(days=day, hours=hour)
            if captured > NOW:
                continue
            db.add(
                AccountMetricSnapshot(
                    x_account_id=account.id,
                    captured_at=captured,
                    followers_count=followers + (per_day_gain if hour == 12 else 0),
                    provenance=Provenance.MEASURED,
                )
            )
        followers += per_day_gain
    await db.flush()


async def add_post(
    db: AsyncSession,
    account: XAccount,
    *,
    posted_at: datetime,
    likes: int = 10,
    impressions: int | None = 1000,
    text: str = "a post",
) -> Post:
    post = Post(
        x_account_id=account.id,
        x_post_id=uuid.uuid4().hex[:12],
        text=text,
        posted_at=posted_at,
        first_seen_at=posted_at,
        metrics_window_closes_at=posted_at + timedelta(days=30),
        char_count=len(text),
    )
    db.add(post)
    await db.flush()
    db.add(
        PostMetricSnapshot(
            post_id=post.id,
            captured_at=posted_at + timedelta(hours=24),
            post_age_hours=24.0,
            like_count=likes,
            impression_count=impressions,
            non_public_available=impressions is not None,
            provenance=Provenance.MEASURED,
        )
    )
    await db.flush()
    return post


class TestFollowerSeries:
    async def test_missing_day_is_a_break_not_a_flat_line(self, db_session: AsyncSession) -> None:
        """The failure this prevents: a chart showing healthy zero-growth days
        across exactly the period when collection was broken."""
        account = await make_account(db_session)
        await add_snapshots(db_session, account, days=10, skip_days={4, 5})

        series = await AnalyticsService(db_session).follower_series(account.id, days=12)

        unobserved = [d for d in series.daily if not d.observed]
        assert len(unobserved) == 2
        assert all(d.followers is None and d.delta is None for d in unobserved)

    async def test_the_day_after_a_gap_has_no_attributable_change(
        self, db_session: AsyncSession
    ) -> None:
        """Two days of growth landing on one day would invent a spike."""
        account = await make_account(db_session)
        await add_snapshots(db_session, account, days=10, skip_days={4})

        series = await AnalyticsService(db_session).follower_series(account.id, days=12)
        days = {d.day: d for d in series.daily}
        missing_day = next(d for d in series.daily if not d.observed)
        after = (date.fromisoformat(missing_day.day) + timedelta(days=1)).isoformat()

        assert days[after].observed is True
        assert days[after].delta is None

    async def test_gaps_are_reported_with_their_length(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)
        await add_snapshots(db_session, account, days=8, skip_days={3})

        series = await AnalyticsService(db_session).follower_series(account.id, days=10)

        assert series.gaps
        assert max(g.hours for g in series.gaps) >= 24
        assert any("break" in c for c in series.caveats)

    async def test_uninterrupted_collection_has_no_gaps(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)
        await add_snapshots(db_session, account, days=8)

        series = await AnalyticsService(db_session).follower_series(account.id, days=10)

        assert all(d.observed for d in series.daily)
        assert all(d.delta is not None for d in series.daily[1:])
        assert series.provenance is Provenance.MEASURED

    async def test_no_history_refuses_rather_than_returning_an_empty_chart(
        self, db_session: AsyncSession
    ) -> None:
        account = await make_account(db_session)
        series = await AnalyticsService(db_session).follower_series(account.id)

        assert series.points == []
        assert any("cannot be backfilled" in c for c in series.caveats)

    async def test_posts_are_counted_against_their_day(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)
        await add_snapshots(db_session, account, days=6)
        posted_at = NOW - timedelta(days=3, hours=2)
        await add_post(db_session, account, posted_at=posted_at)

        series = await AnalyticsService(db_session).follower_series(account.id, days=8)
        day = next(d for d in series.daily if d.day == posted_at.strftime("%Y-%m-%d"))

        assert day.posts == 1


class TestTopicPerformance:
    async def test_unclassified_posts_are_counted_and_disclosed(
        self, db_session: AsyncSession
    ) -> None:
        account = await make_account(db_session)
        topic = Topic(x_account_id=account.id, name="Product")
        db_session.add(topic)
        await db_session.flush()

        for index in range(6):
            post = await add_post(db_session, account, posted_at=NOW - timedelta(days=index + 1))
            if index < 3:
                db_session.add(
                    PostTopic(
                        post_id=post.id,
                        topic_id=topic.id,
                        classified_by="test",
                        provenance=Provenance.INFERRED,
                    )
                )
        await db_session.flush()

        analysis = await AnalyticsService(db_session).topic_performance(account.id, days=30)

        assert analysis.classified_posts == 3
        assert analysis.unclassified_posts == 3
        assert any("carry no topic" in c for c in analysis.caveats)

    async def test_a_thin_topic_is_not_ranked(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)
        topic = Topic(x_account_id=account.id, name="Rare")
        db_session.add(topic)
        await db_session.flush()

        post = await add_post(db_session, account, posted_at=NOW - timedelta(days=1))
        db_session.add(PostTopic(post_id=post.id, topic_id=topic.id, classified_by="test"))
        await db_session.flush()

        analysis = await AnalyticsService(db_session).topic_performance(account.id, days=30)

        assert analysis.topics[0].is_reliable is False
        assert analysis.is_reliable is False
        assert analysis.best is None

    async def test_topics_are_ranked_once_there_is_enough_behind_them(
        self, db_session: AsyncSession
    ) -> None:
        account = await make_account(db_session)
        strong = Topic(x_account_id=account.id, name="Strong")
        weak = Topic(x_account_id=account.id, name="Weak")
        db_session.add_all([strong, weak])
        await db_session.flush()

        for index in range(4):
            hot = await add_post(
                db_session,
                account,
                posted_at=NOW - timedelta(days=index + 1),
                likes=500,
                impressions=1000,
            )
            cold = await add_post(
                db_session,
                account,
                posted_at=NOW - timedelta(days=index + 1, hours=6),
                likes=5,
                impressions=1000,
            )
            db_session.add_all(
                [
                    PostTopic(post_id=hot.id, topic_id=strong.id, classified_by="test"),
                    PostTopic(post_id=cold.id, topic_id=weak.id, classified_by="test"),
                ]
            )
        await db_session.flush()

        analysis = await AnalyticsService(db_session).topic_performance(account.id, days=30)

        assert analysis.is_reliable is True
        assert analysis.best == "Strong"
        assert analysis.worst == "Weak"

    async def test_topic_findings_are_always_inferred(self, db_session: AsyncSession) -> None:
        """Unlike formats, which are detected mechanically and carry no model error."""
        account = await make_account(db_session)
        analysis = await AnalyticsService(db_session).topic_performance(account.id)
        assert analysis.provenance is Provenance.INFERRED

    async def test_no_classification_says_so(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)
        await add_post(db_session, account, posted_at=NOW - timedelta(days=1))

        analysis = await AnalyticsService(db_session).topic_performance(account.id)

        assert analysis.topics == []
        assert any("categorised" in c for c in analysis.caveats)


class TestSeasonality:
    async def test_thin_history_produces_an_unreliable_profile(
        self, db_session: AsyncSession
    ) -> None:
        account = await make_account(db_session)
        await add_snapshots(db_session, account, days=5)

        analysis = await AnalyticsService(db_session).seasonality(account.id, days=30)

        assert analysis.follower_profile.is_reliable is False
        assert any("three weeks" in c for c in analysis.caveats)

    async def test_a_full_month_yields_a_follower_rhythm(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)
        await add_snapshots(db_session, account, days=35)

        analysis = await AnalyticsService(db_session).seasonality(account.id, days=60)

        assert analysis.follower_profile.is_reliable is True
        assert analysis.follower_days_observed >= 28

    async def test_the_two_rhythms_are_kept_separate(self, db_session: AsyncSession) -> None:
        """Which days you gain followers and which days your posts land are
        different questions, and averaging them would hide that."""
        account = await make_account(db_session)
        await add_snapshots(db_session, account, days=35)

        analysis = await AnalyticsService(db_session).seasonality(account.id, days=60)

        assert analysis.follower_profile is not analysis.engagement_profile
        # No posts were added, so engagement has no rhythm to report.
        assert analysis.engagement_profile.is_reliable is False


class TestDashboardSummary:
    async def test_uncollected_impressions_are_null_not_zero(
        self, db_session: AsyncSession
    ) -> None:
        """A zero here would read as 'nobody saw your posts'."""
        account = await make_account(db_session)
        await add_snapshots(db_session, account, days=10)
        for index in range(3):
            await add_post(
                db_session,
                account,
                posted_at=NOW - timedelta(days=index + 1),
                impressions=None,
            )

        summary = await AnalyticsService(db_session).dashboard_summary(account.id)

        assert summary.impressions_total is None
        assert summary.impressions_posts_missing == 3
        assert any("counted as zero" in c for c in summary.caveats)

    async def test_impressions_total_covers_only_measured_posts(
        self, db_session: AsyncSession
    ) -> None:
        account = await make_account(db_session)
        await add_snapshots(db_session, account, days=10)
        await add_post(db_session, account, posted_at=NOW - timedelta(days=1), impressions=800)
        await add_post(db_session, account, posted_at=NOW - timedelta(days=2), impressions=None)

        summary = await AnalyticsService(db_session).dashboard_summary(account.id)

        assert summary.impressions_total == 800
        assert summary.impressions_posts == 1
        assert summary.impressions_posts_missing == 1

    async def test_seven_day_change_is_withheld_until_there_are_seven_days(
        self, db_session: AsyncSession
    ) -> None:
        account = await make_account(db_session)
        await add_snapshots(db_session, account, days=3)

        summary = await AnalyticsService(db_session).dashboard_summary(account.id)

        assert summary.followers_change_7d is None
        assert summary.followers_change_note is not None

    async def test_seven_day_change_appears_with_enough_history(
        self, db_session: AsyncSession
    ) -> None:
        account = await make_account(db_session)
        await add_snapshots(db_session, account, days=14, per_day_gain=5)

        summary = await AnalyticsService(db_session).dashboard_summary(account.id)

        assert summary.followers_change_7d is not None
        assert summary.followers_change_7d > 0

    async def test_top_post_is_chosen_by_rate_not_raw_engagement(
        self, db_session: AsyncSession
    ) -> None:
        """Raw counts just rank by reach, which is not the same question."""
        account = await make_account(db_session)
        await add_snapshots(db_session, account, days=10)
        await add_post(
            db_session,
            account,
            posted_at=NOW - timedelta(days=1),
            likes=100,
            impressions=100_000,
            text="wide reach, poor rate",
        )
        await add_post(
            db_session,
            account,
            posted_at=NOW - timedelta(days=2),
            likes=50,
            impressions=500,
            text="small reach, excellent rate",
        )

        summary = await AnalyticsService(db_session).dashboard_summary(account.id)

        assert summary.top_post is not None
        assert summary.top_post.text == "small reach, excellent rate"

    async def test_revenue_is_summed_from_owner_supplied_entries(
        self, db_session: AsyncSession
    ) -> None:
        account = await make_account(db_session)
        await add_snapshots(db_session, account, days=10)
        db_session.add(
            RevenueEntry(
                x_account_id=account.id,
                source_type=RevenueSourceType.SPONSORSHIP,
                amount_minor=125_000,
                currency="USD",
                earned_at=date.today(),
                provenance=Provenance.USER_ENTERED,
                external_ref="dash-1",
            )
        )
        await db_session.flush()

        summary = await AnalyticsService(db_session).dashboard_summary(account.id)

        assert summary.revenue_total_minor == 125_000
        assert summary.revenue_entries == 1

    async def test_empty_account_returns_a_usable_shape(self, db_session: AsyncSession) -> None:
        """The Overview renders on day one, before anything is collected."""
        account = await make_account(db_session)

        summary = await AnalyticsService(db_session).dashboard_summary(account.id)

        assert summary.followers is None
        assert summary.posts == 0
        assert summary.engagement_rate_median is None
        assert summary.growth_score["score"] is None


class TestDashboardApi:
    async def test_endpoints_require_authentication(self, client) -> None:  # type: ignore[no-untyped-def]
        account_id = uuid.uuid4()
        for path in ("summary", "series", "topics", "seasonality"):
            response = await client.get(f"/api/v1/analytics/{account_id}/{path}")
            assert response.status_code == 401

    async def test_another_users_account_is_not_visible(self, registered_client) -> None:  # type: ignore[no-untyped-def]
        response = await registered_client.get(f"/api/v1/analytics/{uuid.uuid4()}/summary")
        assert response.status_code == 404
