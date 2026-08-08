"""Liveness and readiness probes.

Separated deliberately: `/live` answers "is the process up" (used by Docker to
decide whether to restart), `/ready` answers "can it serve traffic" (used to
decide whether to route to it). Conflating them causes restart loops whenever a
dependency is briefly unavailable.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.api.deps import DbSession
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.ratelimit import redis_healthy

log = get_logger(__name__)
router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def liveness() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/ready")
async def readiness(db: DbSession, response: Response) -> dict[str, Any]:
    checks: dict[str, str] = {}

    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        log.error("health.database_unreachable", error=str(exc))
        checks["database"] = "unreachable"

    checks["redis"] = "ok" if await redis_healthy() else "unreachable"

    # Redis being down degrades rate limiting but does not stop the app serving
    # data, so only the database gates readiness.
    ready = checks["database"] == "ok"
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    settings = get_settings()
    return {
        "status": "ready" if ready else "not_ready",
        "checks": checks,
        "environment": settings.environment,
        "phase": 2,
    }
