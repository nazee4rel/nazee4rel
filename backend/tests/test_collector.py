"""Collector and parsing tests.

The assertions that matter most concern the difference between *absent* and
*zero*. A missing impression count must stay NULL all the way from the X payload
into the database, because once the 30-day window closes there is no way to
recover which of the two it was.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors import schedule
from app.collectors.collector import Collector
from app.collectors.parsing import (
    classify_post_type,
    parse_account_metrics,
    parse_metrics,
    parse_post,
)
from app.integrations.x.client import XApiClient
from app.models.collection import CollectionKind, CollectionStatus
from app.models.content import AccountMetricSnapshot, Post, PostMetricSnapshot, PostType
from app.models.user import User
from app.models.x_account import XAccount
from tests.test_x_client import StubLimiter


async def make_account(db: AsyncSession) -> XAccount:
    user = User(email=f"c{uuid.uuid4().hex[:8]}@example.com", password_hash="x", display_name="C")
    db.add(user)
    await db.flush()
    account = XAccount(
        user_id=user.id,
        x_user_id="555",
        username="collector",
        connected_at=datetime.now(UTC),
    )
    db.add(account)
    await db.flush()
    return account


def collector_for(db: AsyncSession, account: XAccount, handler: object) -> Collector:
    client = XApiClient(
        db,
        account,
        token_provider=_token,
        granted_scopes=("tweet.read", "users.read"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),  # type: ignore[arg-type]
        limiter=StubLimiter(),
    )
    return Collector(db, client)


async def _token() -> str:
    return "collector-token"


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------- parsing
class TestParsing:
    def test_missing_impressions_stay_none_not_zero(self) -> None:
        """The single most important parsing rule in the project."""
        metrics = parse_metrics({"public_metrics": {"like_count": 5}})
        assert metrics["impression_count"] is None
        assert metrics["non_public_available"] is False
        # Public metrics genuinely are zero when absent from the block.
        assert metrics["reply_count"] == 0

    def test_zero_impressions_are_preserved_as_zero(self) -> None:
        """A real zero must survive as 0, not collapse into None."""
        metrics = parse_metrics(
            {"public_metrics": {}, "non_public_metrics": {"impression_count": 0}}
        )
        assert metrics["impression_count"] == 0
        assert metrics["non_public_available"] is True

    def test_full_private_block_is_parsed(self) -> None:
        metrics = parse_metrics(
            {
                "public_metrics": {"like_count": 10, "bookmark_count": 4},
                "non_public_metrics": {
                    "impression_count": 15200,
                    "url_link_clicks": 45,
                    "user_profile_clicks": 90,
                },
                "organic_metrics": {"impression_count": 15000, "like_count": 10},
            }
        )
        assert metrics["impression_count"] == 15200
        assert metrics["url_link_clicks"] == 45
        assert metrics["organic_impression_count"] == 15000
        assert metrics["bookmark_count"] == 4

    def test_post_without_timestamp_is_rejected(self) -> None:
        """Age drives the entire schedule, so a post without one is unusable."""
        assert parse_post({"id": "1", "text": "hi"}) is None

    def test_post_without_id_is_rejected(self) -> None:
        assert parse_post({"created_at": iso(datetime.now(UTC)), "text": "hi"}) is None

    def test_format_features_extracted(self) -> None:
        parsed = parse_post(
            {
                "id": "1",
                "text": "check this out",
                "created_at": iso(datetime.now(UTC)),
                "entities": {"urls": [{"url": "https://t.co/x"}]},
                "attachments": {"media_keys": ["3_1"]},
            }
        )
        assert parsed is not None
        assert parsed["has_link"] is True
        assert parsed["has_media"] is True
        assert parsed["char_count"] == len("check this out")

    def test_post_type_classification(self) -> None:
        assert classify_post_type({}) is PostType.ORIGINAL
        assert classify_post_type({"referenced_tweets": [{"type": "replied_to"}]}) is PostType.REPLY
        assert classify_post_type({"referenced_tweets": [{"type": "quoted"}]}) is PostType.QUOTE
        assert classify_post_type({"referenced_tweets": [{"type": "retweeted"}]}) is PostType.REPOST

    def test_quote_reply_counts_as_reply(self) -> None:
        """More useful for content analysis to treat it as a reply."""
        assert (
            classify_post_type({"referenced_tweets": [{"type": "quoted"}, {"type": "replied_to"}]})
            is PostType.REPLY
        )

    def test_account_metrics_require_follower_count(self) -> None:
        assert parse_account_metrics({"public_metrics": {"following_count": 5}}) is None
        assert parse_account_metrics({}) is None
        parsed = parse_account_metrics({"public_metrics": {"followers_count": 100}})
        assert parsed is not None
        assert parsed["followers_count"] == 100


# ------------------------------------------------------------------ collectors
class TestAccountSnapshot:
    async def test_records_follower_count(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"data": {"id": "555", "public_metrics": {"followers_count": 4213}}},
            )

        collector = collector_for(db_session, account, handler)
        async with collector.client:
            run = await collector.collect_account_snapshot()

        assert run.status is CollectionStatus.SUCCEEDED
        snapshot = await db_session.scalar(select(AccountMetricSnapshot))
        assert snapshot is not None
        assert snapshot.followers_count == 4213

    async def test_is_idempotent_within_the_hour(self, db_session: AsyncSession) -> None:
        """Retries must not produce near-duplicate rows in the follower series."""
        account = await make_account(db_session)

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"data": {"id": "555", "public_metrics": {"followers_count": 100}}}
            )

        collector = collector_for(db_session, account, handler)
        async with collector.client:
            await collector.collect_account_snapshot()
            await collector.collect_account_snapshot()

        count = await db_session.scalar(select(func.count()).select_from(AccountMetricSnapshot))
        assert count == 1

    async def test_missing_metrics_fails_loudly(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": {"id": "555"}})

        collector = collector_for(db_session, account, handler)
        async with collector.client:
            run = await collector.collect_account_snapshot()

        assert run.status is CollectionStatus.FAILED
        assert run.error is not None


class TestPostDiscovery:
    async def test_discovers_and_snapshots_in_one_pass(self, db_session: AsyncSession) -> None:
        """Metrics come from the payload already paid for — no second read."""
        account = await make_account(db_session)
        posted = datetime.now(UTC) - timedelta(hours=3)

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "900",
                            "text": "hello world",
                            "created_at": iso(posted),
                            "public_metrics": {"like_count": 12, "reply_count": 2},
                            "non_public_metrics": {"impression_count": 3400},
                        }
                    ]
                },
            )

        collector = collector_for(db_session, account, handler)
        async with collector.client:
            run = await collector.discover_posts()

        assert run.posts_discovered == 1
        post = await db_session.scalar(select(Post))
        assert post is not None
        assert post.impressions_ever_collected is True
        assert post.discovered_after_window_closed is False

        snapshot = await db_session.scalar(select(PostMetricSnapshot))
        assert snapshot is not None
        assert snapshot.impression_count == 3400
        assert snapshot.like_count == 12

    async def test_old_post_marked_permanently_unavailable(self, db_session: AsyncSession) -> None:
        """Discovered past the cliff: impressions were never obtainable.

        The dashboard must be able to say that, rather than showing a gap that
        looks like a collection failure.
        """
        account = await make_account(db_session)
        old = datetime.now(UTC) - timedelta(days=45)

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "800",
                            "text": "ancient",
                            "created_at": iso(old),
                            "public_metrics": {"like_count": 99},
                        }
                    ]
                },
            )

        collector = collector_for(db_session, account, handler)
        async with collector.client:
            await collector.discover_posts()

        post = await db_session.scalar(select(Post))
        assert post is not None
        assert post.discovered_after_window_closed is True
        assert post.impressions_ever_collected is False

        snapshot = await db_session.scalar(select(PostMetricSnapshot))
        assert snapshot is not None
        # Absent, not zero.
        assert snapshot.impression_count is None
        assert snapshot.like_count == 99

    async def test_existing_posts_are_not_duplicated(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)
        posted = datetime.now(UTC) - timedelta(hours=2)

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "700",
                            "text": "once",
                            "created_at": iso(posted),
                            "public_metrics": {"like_count": 1},
                        }
                    ]
                },
            )

        collector = collector_for(db_session, account, handler)
        async with collector.client:
            await collector.discover_posts()
            await collector.discover_posts()

        assert await db_session.scalar(select(func.count()).select_from(Post)) == 1

    async def test_window_close_recorded_on_discovery(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)
        posted = datetime.now(UTC) - timedelta(days=1)

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "600", "text": "x", "created_at": iso(posted), "public_metrics": {}}
                    ]
                },
            )

        collector = collector_for(db_session, account, handler)
        async with collector.client:
            await collector.discover_posts()

        post = await db_session.scalar(select(Post))
        assert post is not None
        expected = schedule.window_closes_at(posted)
        # SQLite round-trips datetimes as naive; Postgres returns them aware.
        # Normalise so the assertion tests the value, not the driver.
        stored = post.metrics_window_closes_at
        if stored.tzinfo is None:
            stored = stored.replace(tzinfo=UTC)
        assert abs((stored - expected).total_seconds()) < 2


class TestPostMetricsCollection:
    async def test_snapshots_a_due_post(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)
        posted = datetime.now(UTC) - timedelta(hours=5)

        calls = {"n": 0}

        def handler(_r: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "500",
                            "text": "tracked",
                            "created_at": iso(posted),
                            "public_metrics": {"like_count": 10 * calls["n"]},
                            "non_public_metrics": {"impression_count": 1000 * calls["n"]},
                        }
                    ]
                },
            )

        collector = collector_for(db_session, account, handler)
        async with collector.client:
            await collector.discover_posts()
            # Move the existing snapshot back so the post is due again.
            snapshot = await db_session.scalar(select(PostMetricSnapshot))
            assert snapshot is not None
            snapshot.captured_at = datetime.now(UTC) - timedelta(hours=3)
            await db_session.flush()

            run = await collector.collect_post_metrics()

        assert run.snapshots_written == 1
        assert await db_session.scalar(select(func.count()).select_from(PostMetricSnapshot)) == 2

    async def test_freeze_is_marked_and_recorded(self, db_session: AsyncSession) -> None:
        """The pre-cliff capture must set both the flag and the post's timestamp."""
        account = await make_account(db_session)
        posted = datetime.now(UTC) - timedelta(days=29, hours=12)

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "400",
                            "text": "about to expire",
                            "created_at": iso(posted),
                            "public_metrics": {"like_count": 55},
                            "non_public_metrics": {"impression_count": 90000},
                        }
                    ]
                },
            )

        collector = collector_for(db_session, account, handler)
        async with collector.client:
            await collector.discover_posts()
            run = await collector.collect_post_metrics()

        assert run.kind is CollectionKind.FINAL_FREEZE
        post = await db_session.scalar(select(Post))
        assert post is not None
        assert post.final_freeze_at is not None

        frozen = await db_session.scalar(
            select(PostMetricSnapshot).where(PostMetricSnapshot.is_final_freeze.is_(True))
        )
        assert frozen is not None
        assert frozen.impression_count == 90000

    async def test_freeze_bookkeeping_survives_a_same_minute_snapshot(
        self, db_session: AsyncSession
    ) -> None:
        """Regression: discovery and freeze colliding in the same minute.

        The idempotency guard used to return early on a same-minute snapshot,
        skipping the freeze bookkeeping. `final_freeze_at` stayed NULL, so the
        post was re-selected as freeze-due on every cycle — paying for reads
        repeatedly — and was never actually marked as frozen.
        """
        account = await make_account(db_session)
        posted = datetime.now(UTC) - timedelta(days=29, hours=6)

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "410",
                            "text": "expiring",
                            "created_at": iso(posted),
                            "public_metrics": {"like_count": 3},
                            "non_public_metrics": {"impression_count": 7777},
                        }
                    ]
                },
            )

        collector = collector_for(db_session, account, handler)
        async with collector.client:
            # Discovery writes a snapshot for this minute...
            await collector.discover_posts()
            # ...and the freeze lands in the same minute.
            await collector.collect_post_metrics()

        post = await db_session.scalar(select(Post))
        assert post is not None
        assert post.final_freeze_at is not None, "freeze bookkeeping was skipped"

        snapshots = list(await db_session.scalars(select(PostMetricSnapshot)))
        assert len(snapshots) == 1, "no duplicate row should be created"
        assert snapshots[0].is_final_freeze is True
        assert snapshots[0].impression_count == 7777

    async def test_post_past_cliff_is_not_collected(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)
        old = datetime.now(UTC) - timedelta(days=40)

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "300", "text": "old", "created_at": iso(old), "public_metrics": {}}
                    ]
                },
            )

        collector = collector_for(db_session, account, handler)
        async with collector.client:
            await collector.discover_posts()
            run = await collector.collect_post_metrics()

        assert run.status is CollectionStatus.SKIPPED

    async def test_nothing_due_is_skipped_not_failed(self, db_session: AsyncSession) -> None:
        """An idle cycle is a normal outcome, not an error."""
        account = await make_account(db_session)

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": []})

        collector = collector_for(db_session, account, handler)
        async with collector.client:
            run = await collector.collect_post_metrics()

        assert run.status is CollectionStatus.SKIPPED

    async def test_snapshot_write_is_idempotent_within_the_minute(
        self, db_session: AsyncSession
    ) -> None:
        account = await make_account(db_session)
        posted = datetime.now(UTC) - timedelta(hours=2)

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "200",
                            "text": "x",
                            "created_at": iso(posted),
                            "public_metrics": {"like_count": 1},
                        }
                    ]
                },
            )

        collector = collector_for(db_session, account, handler)
        async with collector.client:
            await collector.discover_posts()
            post = await db_session.scalar(select(Post))
            assert post is not None
            now = datetime.now(UTC)
            await collector._write_snapshot(post, {"like_count": 1}, now, is_final_freeze=False)
            await collector._write_snapshot(post, {"like_count": 1}, now, is_final_freeze=False)

        count = await db_session.scalar(select(func.count()).select_from(PostMetricSnapshot))
        assert count == 1


class TestCollectionRunRecording:
    async def test_every_run_is_recorded(self, db_session: AsyncSession) -> None:
        """A silent skip leaves an unexplained hole; runs are always written."""
        account = await make_account(db_session)

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"data": {"id": "555", "public_metrics": {"followers_count": 10}}}
            )

        collector = collector_for(db_session, account, handler)
        async with collector.client:
            run = await collector.collect_account_snapshot()

        assert run.finished_at is not None
        assert run.duration_seconds is not None
        assert run.kind is CollectionKind.ACCOUNT_SNAPSHOT
