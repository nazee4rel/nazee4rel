"""Collection status and manual triggers.

The dashboard needs to answer one question honestly: *is my data actually being
collected?* Silent failure is the expensive outcome here, because impressions
missed inside the 30-day window cannot be recovered afterwards.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession
from app.collectors import schedule
from app.core.errors import NotFoundError
from app.models.collection import CollectionKind, CollectionRun, CollectionStatus
from app.models.content import AccountMetricSnapshot, Post, PostMetricSnapshot
from app.models.x_account import XAccount

router = APIRouter(prefix="/collection", tags=["collection"])


class RunOut(BaseModel):
    id: uuid.UUID
    kind: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    posts_discovered: int
    snapshots_written: int
    skipped_count: int
    degraded_reason: str | None
    error: str | None


class HealthOut(BaseModel):
    """Whether collection is actually working, and what is at risk if not."""

    collecting: bool
    last_account_snapshot_at: datetime | None
    hours_since_account_snapshot: float | None
    posts_tracked: int
    posts_in_window: int
    # Posts inside the freeze window with no freeze yet — the ones whose
    # impressions are about to become unrecoverable.
    posts_awaiting_freeze: int
    posts_frozen: int
    # Discovered only after their window had already closed: their impressions
    # were never obtainable, as opposed to lost through a collection failure.
    posts_impressions_never_available: int
    total_snapshots: int
    warnings: list[str]


async def _account_for(db: DbSession, user: CurrentUser, account_id: uuid.UUID) -> XAccount:
    account = await db.scalar(
        select(XAccount).where(XAccount.id == account_id, XAccount.user_id == user.id)
    )
    if account is None:
        raise NotFoundError("X account not found.")
    return account


@router.get("/{account_id}/health", response_model=HealthOut)
async def collection_health(account_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Any:
    account = await _account_for(db, user, account_id)
    now = datetime.now(UTC)
    warnings: list[str] = []

    last_snapshot = await db.scalar(
        select(func.max(AccountMetricSnapshot.captured_at)).where(
            AccountMetricSnapshot.x_account_id == account.id
        )
    )
    hours_since: float | None = None
    if last_snapshot is not None:
        captured = last_snapshot if last_snapshot.tzinfo else last_snapshot.replace(tzinfo=UTC)
        hours_since = (now - captured).total_seconds() / 3600

    async def count(*where: Any) -> int:
        return int(await db.scalar(select(func.count()).select_from(Post).where(*where)) or 0)

    posts_tracked = await count(Post.x_account_id == account.id)
    posts_in_window = await count(
        Post.x_account_id == account.id, Post.metrics_window_closes_at > now
    )
    awaiting_freeze = await count(
        Post.x_account_id == account.id,
        Post.final_freeze_at.is_(None),
        Post.metrics_window_closes_at > now,
        Post.metrics_window_closes_at
        <= now + timedelta(days=schedule.METRICS_WINDOW_DAYS - schedule.FREEZE_OPENS_AT_DAY),
    )
    frozen = await count(Post.x_account_id == account.id, Post.final_freeze_at.isnot(None))
    never_available = await count(
        Post.x_account_id == account.id, Post.discovered_after_window_closed.is_(True)
    )

    total_snapshots = int(
        await db.scalar(
            select(func.count())
            .select_from(PostMetricSnapshot)
            .join(Post, Post.id == PostMetricSnapshot.post_id)
            .where(Post.x_account_id == account.id)
        )
        or 0
    )

    # Two hours of slack on an hourly job absorbs an ordinary restart without
    # crying wolf.
    collecting = hours_since is not None and hours_since < 2
    if hours_since is None:
        warnings.append(
            "No follower snapshot has been recorded yet. If this persists, the worker "
            "may not be running — every missed hour is permanently lost, since X has "
            "no follower history endpoint."
        )
    elif hours_since >= 2:
        warnings.append(
            f"The last follower snapshot was {hours_since:.1f} hours ago. Collection "
            f"appears to have stopped; check the worker and beat containers."
        )

    if awaiting_freeze:
        warnings.append(
            f"{awaiting_freeze} post(s) are inside the final 24 hours before their "
            f"metrics window closes. Their impressions become permanently "
            f"unavailable once it does."
        )

    return HealthOut(
        collecting=collecting,
        last_account_snapshot_at=last_snapshot,
        hours_since_account_snapshot=round(hours_since, 2) if hours_since is not None else None,
        posts_tracked=posts_tracked,
        posts_in_window=posts_in_window,
        posts_awaiting_freeze=awaiting_freeze,
        posts_frozen=frozen,
        posts_impressions_never_available=never_available,
        total_snapshots=total_snapshots,
        warnings=warnings,
    )


@router.get("/{account_id}/runs", response_model=list[RunOut])
async def recent_runs(
    account_id: uuid.UUID, user: CurrentUser, db: DbSession, limit: int = 25
) -> Any:
    """Recent collection cycles — the raw material for the Agent Activity page."""
    account = await _account_for(db, user, account_id)
    return list(
        await db.scalars(
            select(CollectionRun)
            .where(CollectionRun.x_account_id == account.id)
            .order_by(CollectionRun.started_at.desc())
            .limit(min(limit, 100))
        )
    )


@router.post("/{account_id}/run", response_model=dict)
async def trigger_collection(
    account_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> dict[str, Any]:
    """Run a collection cycle now, in-process.

    Useful for a first run straight after connecting, and for verifying the
    setup without waiting for the beat schedule. Runs inline rather than via
    Celery so the user gets an answer — and any error — immediately.
    """
    from app.collectors.collector import build_collector

    account = await _account_for(db, user, account_id)
    collector = await build_collector(db, account)

    async with collector.client:
        snapshot_run = await collector.collect_account_snapshot()
        discovery_run = await collector.discover_posts()
        metrics_run = await collector.collect_post_metrics()

    return {
        "account_snapshot": {
            "status": snapshot_run.status.value,
            "error": snapshot_run.error,
        },
        "discovery": {
            "status": discovery_run.status.value,
            "posts_discovered": discovery_run.posts_discovered,
            "error": discovery_run.error,
        },
        "metrics": {
            "status": metrics_run.status.value,
            "snapshots_written": metrics_run.snapshots_written,
            "skipped": metrics_run.skipped_count,
            "degraded_reason": metrics_run.degraded_reason,
        },
    }


@router.get("/{account_id}/cost-projection", response_model=dict)
async def cost_projection(account_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Any:
    """Projected monthly API spend at the account's observed posting rate.

    Shown so the cadence's cost is visible before the invoice is, rather than
    after.
    """
    from app.services.cost_service import CostService

    account = await _account_for(db, user, account_id)
    now = datetime.now(UTC)

    recent_posts = int(
        await db.scalar(
            select(func.count())
            .select_from(Post)
            .where(
                Post.x_account_id == account.id,
                Post.posted_at >= now - timedelta(days=30),
            )
        )
        or 0
    )
    posts_per_day = recent_posts / 30 if recent_posts else 0.0

    cost = CostService(db)
    projected_resources = schedule.estimate_monthly_resources(posts_per_day)
    projected_micros = projected_resources * cost.settings.x_cost_owned_read_micros

    return {
        "observed_posts_per_day": round(posts_per_day, 2),
        "snapshots_per_post": schedule.expected_snapshots_per_post(),
        "projected_monthly_resources": projected_resources,
        "projected_monthly_usd": round(projected_micros / 1_000_000, 2),
        "budget": await cost.budget_status(account.id),
        "note": (
            "Projection assumes the current cadence and posting rate. The governor "
            "stretches the cadence as the budget tightens, but never skips the "
            "pre-cliff freeze — that data cannot be re-acquired."
        ),
    }


@router.get("/kinds", response_model=dict)
async def collection_kinds() -> dict[str, list[str]]:
    return {
        "kinds": [k.value for k in CollectionKind],
        "statuses": [s.value for s in CollectionStatus],
    }
