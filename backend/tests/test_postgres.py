"""Tests that only mean anything against real PostgreSQL.

The rest of the suite runs on in-memory SQLite, which is fast, hermetic and
wrong about several things this application depends on:

* `JSONB` is declared with `.with_variant()` and silently degrades to `JSON` on
  SQLite, so nothing else here proves the production column type.
* SQLite returns naive datetimes. Every `_aware()` helper in this codebase
  exists because of that, and nothing else proves those helpers are compensating
  for a test artifact rather than papering over a real bug.
* SQLite serialises writers, so no other test can demonstrate that the unique
  constraints actually arbitrate between two concurrent transactions.
* The migrations are written against the Postgres dialect — enum types,
  `server_default`, JSONB — and until this file existed they had never been
  executed by Postgres at all. `test_migration_parity.py` replays them against a
  recorder, which catches drift from the models but cannot catch invalid SQL.

Skipped unless `TEST_POSTGRES_URL` is set, so `pytest` stays a no-setup command:

    TEST_POSTGRES_URL='postgresql+asyncpg://postgres@/xagent_test?host=/tmp&port=5433' pytest
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.security import uuid7
from app.models import Base
from app.models.alerting import Alert, AlertRuleKey, AlertSeverity, AlertState
from app.models.content import AccountMetricSnapshot, Post, PostMetricSnapshot
from app.models.enums import Provenance
from app.models.user import User
from app.models.x_account import XAccount

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL", "")

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL, reason="TEST_POSTGRES_URL is not set; skipping PostgreSQL-only tests"
)


def _run_migrations(sync_conn: sa.Connection) -> None:
    """Apply every migration to the connection Alembic is handed."""
    from alembic.config import Config

    from alembic import command

    config = Config("alembic.ini")
    config.attributes["connection"] = sync_conn
    command.upgrade(config, "head")


def _reset_schema(sync_conn: sa.Connection) -> None:
    sync_conn.execute(sa.text("DROP SCHEMA public CASCADE"))
    sync_conn.execute(sa.text("CREATE SCHEMA public"))


async def _build_schema() -> None:
    engine = create_async_engine(POSTGRES_URL)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(_reset_schema)
        async with engine.begin() as conn:
            await conn.run_sync(_run_migrations)
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def pg_schema() -> None:
    """A database built the way production is: by running the migrations.

    Deliberately not `Base.metadata.create_all`. Creating the schema from the
    models would test the models against themselves, and would never catch a
    migration that does not run.

    Synchronous and module-scoped so it owns its own event loop; the async
    fixtures below are function-scoped and create their own engines, which keeps
    every connection on the loop that will use it.
    """
    asyncio.run(_build_schema())


@pytest.fixture
async def pg_engine(pg_schema: None) -> AsyncGenerator[object, None]:
    engine = create_async_engine(POSTGRES_URL)
    yield engine
    await engine.dispose()


@pytest.fixture
async def pg_session(pg_engine) -> AsyncGenerator[AsyncSession, None]:  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(bind=pg_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
        await session.rollback()


async def make_account(db: AsyncSession) -> XAccount:
    user = User(
        email=f"pg{uuid.uuid4().hex[:10]}@example.com",
        password_hash="x",
        display_name="PG",
        timezone="UTC",
    )
    db.add(user)
    await db.flush()
    account = XAccount(
        user_id=user.id,
        x_user_id=uuid.uuid4().hex[:12],
        username=f"pg{uuid.uuid4().hex[:6]}",
        connected_at=datetime.now(UTC),
    )
    db.add(account)
    await db.flush()
    return account


class TestSchemaMatchesModels:
    """The live schema, not a replayed recording of it."""

    async def test_migrated_tables_match_the_models(self, pg_engine) -> None:  # type: ignore[no-untyped-def]
        def check(sync_conn: sa.Connection) -> None:
            inspector = sa.inspect(sync_conn)
            live = {t for t in inspector.get_table_names() if t != "alembic_version"}
            assert live == set(Base.metadata.tables)

        async with pg_engine.begin() as conn:
            await conn.run_sync(check)

    async def test_every_column_matches_including_nullability(self, pg_engine) -> None:  # type: ignore[no-untyped-def]
        """The parity test compares names. This compares what Postgres built."""

        def check(sync_conn: sa.Connection) -> None:
            inspector = sa.inspect(sync_conn)
            problems: list[str] = []
            for table in sorted(set(Base.metadata.tables)):
                live = {c["name"]: c for c in inspector.get_columns(table)}
                model = {c.name: c for c in Base.metadata.tables[table].columns}
                for name in set(model) ^ set(live):
                    problems.append(f"{table}.{name}: present on one side only")
                for name in set(model) & set(live):
                    if bool(model[name].nullable) != bool(live[name]["nullable"]):
                        problems.append(
                            f"{table}.{name}: nullable model={model[name].nullable} "
                            f"db={live[name]['nullable']}"
                        )
            assert not problems, "\n".join(problems)

        async with pg_engine.begin() as conn:
            await conn.run_sync(check)

    async def test_json_columns_are_jsonb(self, pg_engine) -> None:  # type: ignore[no-untyped-def]
        """`.with_variant()` degrades to JSON on SQLite, so only Postgres shows this.

        It matters: JSONB is the type that can be indexed and queried, and every
        evidence bundle, alert fact set and report section lives in one.
        """

        def check(sync_conn: sa.Connection) -> None:
            inspector = sa.inspect(sync_conn)
            json_columns = [
                (table, column["name"], str(column["type"]).upper())
                for table in inspector.get_table_names()
                for column in inspector.get_columns(table)
                if "JSON" in str(column["type"]).upper()
            ]
            assert json_columns, "expected JSON columns to exist"
            plain = [c for c in json_columns if c[2] != "JSONB"]
            assert not plain, f"columns that are JSON rather than JSONB: {plain}"

        async with pg_engine.begin() as conn:
            await conn.run_sync(check)


class TestMigrationsAreReversible:
    async def test_downgrade_then_upgrade_leaves_no_residue(self, pg_schema: None) -> None:
        """The rollback path, executed rather than asserted.

        Postgres keeps enum types after their tables are dropped, so a downgrade
        that forgets them makes the next upgrade fail with "type already exists".
        Every migration's downgrade drops its own types; this proves it.
        """
        engine = create_async_engine(POSTGRES_URL)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(_reset_schema)
            async with engine.begin() as conn:
                await conn.run_sync(_run_migrations)

            def downgrade(sync_conn: sa.Connection) -> None:
                from alembic.config import Config

                from alembic import command

                config = Config("alembic.ini")
                config.attributes["connection"] = sync_conn
                command.downgrade(config, "base")

            async with engine.begin() as conn:
                await conn.run_sync(downgrade)

            def assert_clean(sync_conn: sa.Connection) -> None:
                tables = [
                    t for t in sa.inspect(sync_conn).get_table_names() if t != "alembic_version"
                ]
                assert tables == [], f"downgrade left tables behind: {tables}"
                leftover = (
                    sync_conn.execute(
                        sa.text(
                            "SELECT t.typname FROM pg_type t "
                            "JOIN pg_namespace n ON n.oid = t.typnamespace "
                            "WHERE t.typtype = 'e' AND n.nspname = 'public'"
                        )
                    )
                    .scalars()
                    .all()
                )
                assert leftover == [], f"downgrade left enum types behind: {leftover}"

            async with engine.begin() as conn:
                await conn.run_sync(assert_clean)

            # And it must be possible to come back up.
            async with engine.begin() as conn:
                await conn.run_sync(_run_migrations)
        finally:
            # Leave the database usable for the module-scoped fixture.
            async with engine.begin() as conn:
                await conn.run_sync(_reset_schema)
            async with engine.begin() as conn:
                await conn.run_sync(_run_migrations)
            await engine.dispose()


class TestTimezoneHandling:
    async def test_datetimes_come_back_timezone_aware(self, pg_session: AsyncSession) -> None:
        """SQLite returns naive datetimes; Postgres does not.

        Every `_aware()` helper in this codebase exists to normalise the SQLite
        case. If Postgres also returned naive values those helpers would be
        hiding a real bug rather than compensating for a test artifact.
        """
        account = await make_account(pg_session)
        captured = datetime.now(UTC).replace(microsecond=0)
        pg_session.add(
            AccountMetricSnapshot(
                x_account_id=account.id,
                captured_at=captured,
                followers_count=100,
                provenance=Provenance.MEASURED,
            )
        )
        await pg_session.flush()
        pg_session.expunge_all()

        stored = await pg_session.scalar(
            sa.select(AccountMetricSnapshot).where(AccountMetricSnapshot.x_account_id == account.id)
        )
        assert stored is not None
        assert stored.captured_at.tzinfo is not None
        assert stored.captured_at == captured


class TestConstraintsUnderConcurrency:
    async def test_two_transactions_cannot_write_the_same_snapshot(self, pg_engine) -> None:  # type: ignore[no-untyped-def]
        """The idempotency guarantee, arbitrated by the database.

        The collector relies on `uq_snapshot_post_time` to make retries safe. On
        SQLite writers are serialised, so no other test can show two concurrent
        transactions racing for the same row — here one commits and the other
        gets an IntegrityError, which is exactly the behaviour the retry logic
        assumes.
        """
        maker = async_sessionmaker(bind=pg_engine, class_=AsyncSession, expire_on_commit=False)

        async with maker() as setup:
            account = await make_account(setup)
            posted_at = datetime.now(UTC) - timedelta(days=1)
            post = Post(
                x_account_id=account.id,
                x_post_id=uuid.uuid4().hex[:12],
                text="race",
                posted_at=posted_at,
                first_seen_at=posted_at,
                metrics_window_closes_at=posted_at + timedelta(days=30),
            )
            setup.add(post)
            await setup.commit()
            post_id, captured = post.id, datetime.now(UTC).replace(microsecond=0)

        def snapshot() -> PostMetricSnapshot:
            return PostMetricSnapshot(
                post_id=post_id,
                captured_at=captured,
                post_age_hours=24.0,
                provenance=Provenance.MEASURED,
            )

        async def write() -> str:
            async with maker() as session:
                try:
                    session.add(snapshot())
                    await session.commit()
                    return "committed"
                except IntegrityError:
                    await session.rollback()
                    return "rejected"

        outcomes = await asyncio.gather(write(), write())

        assert sorted(outcomes) == ["committed", "rejected"]

    async def test_alert_dedupe_is_enforced_by_the_database(self, pg_engine) -> None:  # type: ignore[no-untyped-def]
        """Two evaluations racing must not produce two alerts for one event."""
        maker = async_sessionmaker(bind=pg_engine, class_=AsyncSession, expire_on_commit=False)

        async with maker() as setup:
            account = await make_account(setup)
            await setup.commit()
            account_id = account.id

        dedupe_key = f"COLLECTION_STALLED:{uuid.uuid4().hex[:8]}"

        async def raise_alert() -> str:
            async with maker() as session:
                try:
                    session.add(
                        Alert(
                            x_account_id=account_id,
                            rule_key=AlertRuleKey.COLLECTION_STALLED,
                            severity=AlertSeverity.CRITICAL,
                            state=AlertState.FIRING,
                            title="Collection has stopped",
                            body="…",
                            dedupe_key=dedupe_key,
                            fired_at=datetime.now(UTC),
                        )
                    )
                    await session.commit()
                    return "committed"
                except IntegrityError:
                    await session.rollback()
                    return "rejected"

        outcomes = await asyncio.gather(raise_alert(), raise_alert())
        assert sorted(outcomes) == ["committed", "rejected"]


class TestUuidPrimaryKeys:
    async def test_uuid7_keys_sort_by_insertion_time_in_postgres(
        self, pg_session: AsyncSession
    ) -> None:
        """The reason these tables use UUIDv7 rather than v4.

        Snapshot tables are append-only and read in time order; time-sortable
        keys keep inserts at the right-hand edge of the index instead of
        scattering them. That property has to survive Postgres's `uuid` type,
        not just Python's `uuid.UUID`.
        """
        account = await make_account(pg_session)
        ids = []
        for hour in range(10):
            row = AccountMetricSnapshot(
                id=uuid7(),
                x_account_id=account.id,
                captured_at=datetime.now(UTC) - timedelta(hours=hour),
                followers_count=1000 + hour,
                provenance=Provenance.MEASURED,
            )
            pg_session.add(row)
            ids.append(row.id)
        await pg_session.flush()

        ordered = list(
            await pg_session.scalars(
                sa.select(AccountMetricSnapshot.id)
                .where(AccountMetricSnapshot.x_account_id == account.id)
                .order_by(AccountMetricSnapshot.id)
            )
        )
        assert ordered == ids


class TestEnumsAreNativeTypes:
    async def test_provenance_is_a_postgres_enum(self, pg_engine) -> None:  # type: ignore[no-untyped-def]
        """A CHECK-constrained varchar would accept a typo at the edges.

        The whole provenance design rests on the column refusing anything
        outside the set, so this confirms Postgres is enforcing it.
        """

        def check(sync_conn: sa.Connection) -> None:
            labels = (
                sync_conn.execute(
                    sa.text(
                        "SELECT e.enumlabel FROM pg_enum e "
                        "JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = 'provenance' "
                        "ORDER BY e.enumsortorder"
                    )
                )
                .scalars()
                .all()
            )
            assert labels == [p.value for p in Provenance]

        async with pg_engine.begin() as conn:
            await conn.run_sync(check)

    async def test_an_invalid_provenance_is_rejected(self, pg_session: AsyncSession) -> None:
        account = await make_account(pg_session)
        with pytest.raises(Exception) as caught:
            await pg_session.execute(
                sa.text(
                    "INSERT INTO account_metric_snapshots "
                    "(id, x_account_id, captured_at, followers_count, provenance, "
                    " created_at, updated_at) "
                    "VALUES (:id, :account, now(), 1, 'GUESSED', now(), now())"
                ),
                {"id": uuid7(), "account": account.id},
            )
        assert "GUESSED" in str(caught.value) or "invalid input value" in str(caught.value)
        await pg_session.rollback()


class TestWorkerEventLoopIsolation:
    """The Celery bridge, exercised the way a real worker exercises it.

    `run_async` opens a fresh event loop per task, but the engine is a module
    global and its pooled asyncpg connections stay bound to the loop that opened
    them. Without disposing the pool, the second task in a worker process checks
    out a connection whose loop has been closed and dies with "got Future
    attached to a different loop".

    Nothing else in the suite can catch this. `test_worker.py` substitutes
    `session_scope` for a SQLite-backed one, and aiosqlite tolerates cross-loop
    reuse because each connection lives on its own thread. It takes real asyncpg
    and a real second task to reproduce, which is exactly what this does.

    The failure matters more than it looks: the worker would finish its first
    task and fail every one afterwards. For an hourly collector whose data
    cannot be re-fetched after 30 days, that is silent, permanent loss behind a
    process that is still up.
    """

    def test_a_second_task_does_not_inherit_a_dead_loops_connections(
        self, pg_schema: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core.config import get_settings
        from app.db import session as db_session
        from app.worker import tasks

        monkeypatch.setattr(get_settings(), "database_url_override", POSTGRES_URL)
        # Deliberately not `dispose_engine()`: this test is about the global
        # engine, so it starts and ends with the globals cleared.
        monkeypatch.setattr(db_session, "_engine", None)
        monkeypatch.setattr(db_session, "_sessionmaker", None)

        first = tasks.cleanup_oauth_states.run()
        second = tasks.cleanup_oauth_states.run()
        third = tasks.cleanup_oauth_states.run()

        assert first == second == third == {"deleted": 0}

    def test_the_pool_is_emptied_between_tasks(
        self, pg_schema: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The mechanism, not just the symptom.

        Asserting on the pool directly means a future change that keeps tasks
        working by some other route still has to say so explicitly here.
        """
        from app.core.config import get_settings
        from app.db import session as db_session

        monkeypatch.setattr(get_settings(), "database_url_override", POSTGRES_URL)
        monkeypatch.setattr(db_session, "_engine", None)
        monkeypatch.setattr(db_session, "_sessionmaker", None)

        from app.worker import tasks

        tasks.cleanup_oauth_states.run()
        # `run_async` clears the globals on its way out, so there is no pool left
        # holding connections bound to the loop that has just closed.
        assert db_session._engine is None
        assert db_session._sessionmaker is None
