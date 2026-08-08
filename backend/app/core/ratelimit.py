"""Redis-backed fixed-window rate limiting for inbound requests.

Phase 3 adds a separate outbound token bucket for calls *to* X, which has
different requirements (it is primed from `x-rate-limit-*` response headers
rather than configured up front). This module covers inbound traffic only.

Design note: if Redis is unavailable the limiter fails **open** and logs an
error. For a single-user analytics dashboard, locking the owner out of their own
data because a cache container restarted is the worse failure. The login
endpoint is the exception worth watching — it is also protected by Argon2's
cost, which makes brute force expensive regardless.
"""

from __future__ import annotations

import time

import redis.asyncio as aioredis

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(
            get_settings().redis_url, encoding="utf-8", decode_responses=True
        )
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


async def check_rate_limit(key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    """Return (allowed, seconds_until_reset).

    Fixed window: cheap, and precise enough for abuse prevention. A sliding
    window would smooth boundary bursts but costs more per request than it is
    worth here.
    """
    window = int(time.time()) // window_seconds
    redis_key = f"ratelimit:{key}:{window}"
    reset_in = window_seconds - (int(time.time()) % window_seconds)

    try:
        client = get_redis()
        pipe = client.pipeline()
        pipe.incr(redis_key)
        pipe.expire(redis_key, window_seconds)
        count, _ = await pipe.execute()
        return int(count) <= limit, reset_in
    except Exception as exc:  # noqa: BLE001
        log.error("ratelimit.unavailable", error=str(exc), key=key)
        return True, reset_in  # fail open — see module docstring


async def redis_healthy() -> bool:
    try:
        return bool(await get_redis().ping())
    except Exception:  # noqa: BLE001
        return False
