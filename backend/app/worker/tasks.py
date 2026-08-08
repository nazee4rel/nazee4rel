"""Celery tasks.

Each task opens its own database session and iterates the active accounts. One
account failing must never stop the others, so every per-account call is
individually guarded — a single expired token should not take down collection
for the rest.

Tasks are thin wrappers. All the judgement lives in `app.collectors`, which is
testable without a broker.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select

from app.collectors.collector import Collector, active_account_ids, build_collector
from app.core.logging import get_logger
from app.db.session import session_scope
from app.models.usage import OAuthState
from app.models.x_account import XAccount
from app.worker.celery_app import celery_app

log = get_logger(__name__)


def run_async[T](coro: Awaitable[T]) -> T:
    """Bridge Celery's synchronous worker into the async codebase.

    A fresh event loop per task keeps tasks isolated from each other and avoids
    reusing a loop left in a bad state by a previous failure.
    """
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


async def _for_each_account(
    operation: Callable[[Collector], Awaitable[Any]], label: str
) -> dict[str, Any]:
    """Run one collector operation across every active account."""
    summary: dict[str, Any] = {"accounts": 0, "succeeded": 0, "failed": 0, "results": []}

    async with session_scope() as db:
        account_ids = await active_account_ids(db)

    summary["accounts"] = len(account_ids)

    for account_id in account_ids:
        # A session per account: one failure rolls back only its own work.
        try:
            async with session_scope() as db:
                account = await db.get(XAccount, account_id)
                if account is None:
                    continue
                collector = await build_collector(db, account)
                async with collector.client:
                    run = await operation(collector)
                summary["succeeded"] += 1
                summary["results"].append(
                    {
                        "account": account.username,
                        "status": run.status.value,
                        "snapshots": run.snapshots_written,
                        "discovered": run.posts_discovered,
                        "skipped": run.skipped_count,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            summary["failed"] += 1
            log.exception(label + ".account_failed", account_id=str(account_id), error=str(exc))

    log.info(label + ".completed", **{k: v for k, v in summary.items() if k != "results"})
    return summary


@celery_app.task(name="collect.account_snapshots")
def collect_account_snapshots() -> dict[str, Any]:
    """Hourly follower count. X has no history endpoint — this row is it."""
    return run_async(
        _for_each_account(lambda c: c.collect_account_snapshot(), "collect.account_snapshots")
    )


@celery_app.task(name="collect.discover_posts")
def discover_posts() -> dict[str, Any]:
    return run_async(_for_each_account(lambda c: c.discover_posts(), "collect.discover_posts"))


@celery_app.task(name="collect.post_metrics")
def collect_post_metrics() -> dict[str, Any]:
    """Snapshot posts due on the decay ladder."""
    return run_async(_for_each_account(lambda c: c.collect_post_metrics(), "collect.post_metrics"))


@celery_app.task(name="collect.freeze_sweep")
def freeze_sweep() -> dict[str, Any]:
    """Dedicated safety net for posts approaching the 30-day cliff.

    Deliberately duplicates work `collect.post_metrics` would do anyway. The
    duplication is the point: a freeze missed because the ordinary cycle was
    degraded, rate-limited or failing costs those impressions permanently, so
    it gets a second independent attempt on its own schedule.
    """
    return run_async(_for_each_account(lambda c: c.collect_post_metrics(), "collect.freeze_sweep"))


@celery_app.task(name="collect.probe_capabilities")
def probe_capabilities() -> dict[str, Any]:
    """Weekly re-probe. X changes access rules; the matrix should not go stale."""

    async def _run() -> dict[str, Any]:
        from app.integrations.x.probe import probe_account

        summary: dict[str, Any] = {"accounts": 0, "failed": 0}
        async with session_scope() as db:
            account_ids = await active_account_ids(db)
        summary["accounts"] = len(account_ids)

        for account_id in account_ids:
            try:
                async with session_scope() as db:
                    account = await db.get(XAccount, account_id)
                    if account is not None:
                        await probe_account(db, account)
            except Exception as exc:  # noqa: BLE001
                summary["failed"] += 1
                log.exception("probe.failed", account_id=str(account_id), error=str(exc))
        return summary

    return run_async(_run())


@celery_app.task(name="maintenance.cleanup_oauth_states")
def cleanup_oauth_states() -> dict[str, int]:
    """Delete spent and expired OAuth handshakes.

    A pending authorization is an unused credential sitting in the database;
    there is no reason to keep them past their usefulness.
    """

    async def _run() -> dict[str, int]:
        cutoff = datetime.now(UTC) - timedelta(days=1)
        async with session_scope() as db:
            result = await db.execute(delete(OAuthState).where(OAuthState.expires_at < cutoff))
            return {"deleted": int(getattr(result, "rowcount", 0) or 0)}

    return run_async(_run())


@celery_app.task(name="collect.backfill_account")
def backfill_account(account_id: str) -> dict[str, Any]:
    """One-off collection right after connecting.

    Queued from the OAuth callback so the dataset starts accumulating
    immediately rather than waiting for the next beat tick — posts already past
    30 days can never have their impressions recovered, so every hour counts on
    day one.
    """

    async def _run() -> dict[str, Any]:
        async with session_scope() as db:
            account = await db.get(XAccount, uuid.UUID(account_id))
            if account is None:
                return {"error": "account not found"}
            collector = await build_collector(db, account)
            async with collector.client:
                snapshot_run = await collector.collect_account_snapshot()
                discovery_run = await collector.discover_posts(max_pages=3)
            return {
                "account": account.username,
                "snapshot": snapshot_run.status.value,
                "discovery": discovery_run.status.value,
                "posts_discovered": discovery_run.posts_discovered,
            }

    return run_async(_run())


@celery_app.task(name="maintenance.stale_account_check")
def stale_account_check() -> dict[str, Any]:
    """Flag accounts whose collection has silently stopped.

    Worth its own check because the failure is invisible otherwise: collection
    quietly stopping looks exactly like an account that simply is not posting,
    until you notice a month of missing impressions that cannot be recovered.
    """

    async def _run() -> dict[str, Any]:
        from app.models.collection import CollectionRun

        cutoff = datetime.now(UTC) - timedelta(hours=6)
        stale: list[str] = []

        async with session_scope() as db:
            for account_id in await active_account_ids(db):
                latest = await db.scalar(
                    select(CollectionRun.started_at)
                    .where(CollectionRun.x_account_id == account_id)
                    .order_by(CollectionRun.started_at.desc())
                    .limit(1)
                )
                if latest is None or latest.replace(tzinfo=UTC) < cutoff:
                    stale.append(str(account_id))
                    log.error("collection.stale", account_id=str(account_id))

        return {"stale_accounts": stale}

    return run_async(_run())
