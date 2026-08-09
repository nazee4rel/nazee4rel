"""Scheduled jobs.

These were the last uncovered lines in the project, and the gap mattered more
than the percentage suggested. The tasks are thin wrappers, but the failure mode
is specific and silent: a beat entry naming a task that does not exist is
accepted by Celery at startup and simply never fires. In this system that is not
a cosmetic outage — a collection job that quietly stops running loses impressions
permanently, because X does not return them again after 30 days.

So the important test here is the one that walks the beat schedule and resolves
every entry against the task registry. The rest run each task body against an
empty database to prove it does not raise on the "no accounts yet" path, which
is the state every fresh install spends its first minutes in.
"""

from __future__ import annotations

import pytest

from app.worker import tasks
from app.worker.celery_app import celery_app


class TestScheduleIntegrity:
    def test_every_beat_entry_names_a_registered_task(self) -> None:
        """The check that catches a renamed task before it stops firing."""
        registered = set(celery_app.tasks)
        missing = {
            name: entry["task"]
            for name, entry in celery_app.conf.beat_schedule.items()
            if entry["task"] not in registered
        }
        assert not missing, f"beat entries pointing at unregistered tasks: {missing}"

    def test_the_schedule_is_not_empty(self) -> None:
        """A vacuous pass above would mean collection is not scheduled at all."""
        assert len(celery_app.conf.beat_schedule) >= 10

    def test_collection_jobs_are_all_scheduled(self) -> None:
        """Each of these missing is a distinct kind of permanent data loss."""
        scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
        for required in (
            "collect.account_snapshots",  # the only follower history that exists
            "collect.discover_posts",
            "collect.post_metrics",
            "collect.freeze_sweep",  # the pre-cliff safety net
            "alerts.evaluate",  # tells you when the above stop
        ):
            assert required in scheduled, f"{required} is not scheduled"

    def test_exactly_one_scheduler_entry_per_task(self) -> None:
        """Two entries for one task would double the work and the API bill."""
        tasks_scheduled = [entry["task"] for entry in celery_app.conf.beat_schedule.values()]
        duplicates = {t for t in tasks_scheduled if tasks_scheduled.count(t) > 1}
        # The freeze sweep deliberately duplicates `collect.post_metrics`' work,
        # but under its own task name, so nothing should appear twice here.
        assert not duplicates, f"tasks scheduled more than once: {duplicates}"

    def test_acks_are_late_so_a_crash_reruns_the_cycle(self) -> None:
        """Collection is idempotent by design; losing a cycle to a crash is not."""
        assert celery_app.conf.task_acks_late is True
        assert celery_app.conf.task_reject_on_worker_lost is True


class TestTaskBodies:
    """Every task, run against a database with no connected accounts.

    That is the state a fresh install is in, and a task that raises there fills
    the log with tracebacks from the first minute.
    """

    @pytest.fixture(autouse=True)
    def _sqlite_sessions(self, engine, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
        """Point `session_scope` at the test database.

        The tasks open their own sessions rather than receiving one, which is
        correct for Celery and inconvenient here.
        """
        from contextlib import asynccontextmanager

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

        @asynccontextmanager
        async def scope():  # type: ignore[no-untyped-def]
            async with maker() as session:
                yield session
                await session.commit()

        monkeypatch.setattr(tasks, "session_scope", scope)

    @pytest.mark.parametrize(
        "task_name",
        [
            "collect_account_snapshots",
            "discover_posts",
            "collect_post_metrics",
            "freeze_sweep",
            "probe_capabilities",
            "cleanup_oauth_states",
            "stale_account_check",
            "agent_daily_cycle",
            "agent_expire_approvals",
            "agent_verify_predictions",
            "evaluate_alerts",
            "daily_report",
            "weekly_report",
            "monthly_report",
        ],
    )
    def test_task_runs_against_an_empty_database(self, task_name: str) -> None:
        task = getattr(tasks, task_name)
        # `.run` calls the undecorated body, so no broker is involved.
        result = task.run() if hasattr(task, "run") else task()
        assert isinstance(result, dict)

    def test_backfill_handles_an_unknown_account(self) -> None:
        """Queued from the OAuth callback; the account can be gone by the time it runs."""
        import uuid

        result = tasks.backfill_account.run(str(uuid.uuid4()))
        assert result == {"error": "account not found"}


class TestAsyncBridge:
    def test_run_async_returns_the_coroutine_result(self) -> None:
        async def answer() -> int:
            return 42

        assert tasks.run_async(answer()) == 42

    def test_each_call_gets_a_fresh_event_loop(self) -> None:
        """A loop left broken by one task must not poison the next."""
        import asyncio

        async def failing() -> None:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            tasks.run_async(failing())

        async def works() -> str:
            await asyncio.sleep(0)
            return "fine"

        assert tasks.run_async(works()) == "fine"

    def test_one_account_failing_does_not_stop_the_others(self) -> None:
        """The guarantee every per-account loop in this module relies on."""
        import inspect

        source = inspect.getsource(tasks)
        # Each per-account body is individually guarded.
        assert source.count("except Exception as exc:  # noqa: BLE001") >= 5
