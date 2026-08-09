"""Outbound rate limiting for calls to X.

Distinct from the inbound limiter in `app.core.ratelimit`.

The limits are not configured. X publishes them per endpoint and changes them,
so the bucket is primed from the `x-rate-limit-*` headers on real responses and
simply believes what X says.

**Behaviour when Redis is unavailable.** Neither extreme is right here, and the
reason is specific to this application. Failing closed halts collection — and
because X drops `non_public_metrics` for posts older than 30 days, a long enough
halt destroys impression data that can never be re-fetched at any price. Failing
fully open risks sustained 429s. So the limiter degrades to a process-local
fallback instead: weaker than the shared bucket (it cannot see other workers)
but far better than either guessing or stopping. A 429 is a normal, handled
response; permanent data loss is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import redis.asyncio as aioredis

from app.core.logging import get_logger
from app.core.ratelimit import get_redis

log = get_logger(__name__)

_KEY_PREFIX = "xratelimit"
# How long a learned limit is trusted before we probe again by simply allowing
# a call through. Comfortably longer than X's 15-minute windows.
_STATE_TTL_SECONDS = 3600

# Process-local fallback used only while Redis is unreachable. Not shared
# between workers, which is precisely why it is a fallback and not the design.
_local_state: dict[str, tuple[int | None, datetime | None]] = {}


@dataclass(frozen=True)
class RateLimitState:
    remaining: int | None
    reset_at: datetime | None

    @property
    def is_exhausted(self) -> bool:
        if self.remaining is None:
            return False
        if self.remaining > 0:
            return False
        # Exhausted, but only until the window resets.
        if self.reset_at is None:
            return True
        return self.reset_at > datetime.now(UTC)

    @property
    def seconds_until_reset(self) -> int:
        if self.reset_at is None:
            return 0
        return max(0, int((self.reset_at - datetime.now(UTC)).total_seconds()))


def _key(account_id: str, endpoint_key: str) -> str:
    return f"{_KEY_PREFIX}:{account_id}:{endpoint_key}"


def parse_headers(headers: object) -> RateLimitState:
    """Read X's rate-limit headers from a response.

    Header absence is meaningful and distinct from zero: it means X told us
    nothing, so we make no assumption rather than inventing a limit.
    """
    get = getattr(headers, "get", None)
    if get is None:
        return RateLimitState(None, None)

    remaining_raw = get("x-rate-limit-remaining")
    reset_raw = get("x-rate-limit-reset")

    remaining: int | None = None
    if remaining_raw is not None:
        try:
            remaining = int(remaining_raw)
        except (TypeError, ValueError):
            remaining = None

    reset_at: datetime | None = None
    if reset_raw is not None:
        try:
            # X sends a Unix epoch second.
            reset_at = datetime.fromtimestamp(int(reset_raw), tz=UTC)
        except (TypeError, ValueError, OSError):
            reset_at = None

    return RateLimitState(remaining=remaining, reset_at=reset_at)


class OutboundRateLimiter:
    def __init__(self, redis: aioredis.Redis | None = None) -> None:
        self._redis = redis

    @property
    def redis(self) -> aioredis.Redis:
        return self._redis if self._redis is not None else get_redis()

    async def record(self, account_id: str, endpoint_key: str, state: RateLimitState) -> None:
        """Store what X reported about our remaining budget."""
        if state.remaining is None:
            return
        payload = {
            "remaining": str(state.remaining),
            "reset_at": str(int(state.reset_at.timestamp())) if state.reset_at else "",
        }
        key = _key(account_id, endpoint_key)
        # Always mirror locally, so a Redis failure mid-cycle still has state.
        _local_state[key] = (state.remaining, state.reset_at)
        try:
            async with self.redis.pipeline() as pipe:
                pipe.hset(key, mapping=payload)
                pipe.expire(key, _STATE_TTL_SECONDS)
                await pipe.execute()
        except Exception as exc:  # noqa: BLE001
            log.warning("x.ratelimit.record_failed", error=str(exc), endpoint=endpoint_key)

    async def check(self, account_id: str, endpoint_key: str) -> tuple[bool, int]:
        """Return (allowed, seconds_to_wait).

        Degrades to the process-local fallback when Redis is unreachable rather
        than halting collection — see the module docstring for why that trade
        goes this way in this application.
        """
        key = _key(account_id, endpoint_key)
        try:
            raw = await self.redis.hgetall(key)  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001
            log.error("x.ratelimit.degraded_to_local", error=str(exc), endpoint=endpoint_key)
            local_remaining, local_reset = _local_state.get(key, (None, None))
            local = RateLimitState(remaining=local_remaining, reset_at=local_reset)
            if local.is_exhausted:
                return False, local.seconds_until_reset + 1
            return True, 0

        if not raw:
            # Nothing learned yet. Allow one call through so the response can
            # teach us the real limit.
            return True, 0

        remaining_raw = raw.get("remaining")
        reset_raw = raw.get("reset_at") or ""
        try:
            remaining = int(remaining_raw) if remaining_raw is not None else None
        except (TypeError, ValueError):
            remaining = None

        reset_at: datetime | None = None
        if reset_raw:
            try:
                reset_at = datetime.fromtimestamp(int(reset_raw), tz=UTC)
            except (TypeError, ValueError, OSError):
                reset_at = None

        state = RateLimitState(remaining=remaining, reset_at=reset_at)
        if state.is_exhausted:
            wait = state.seconds_until_reset
            log.info(
                "x.ratelimit.exhausted",
                endpoint=endpoint_key,
                seconds_until_reset=wait,
            )
            # +1 so we resume just after the window turns, not exactly on it.
            return False, wait + 1

        return True, 0

    async def consume(self, account_id: str, endpoint_key: str) -> None:
        """Optimistically decrement before a request.

        Prevents a burst of concurrent workers from all seeing the same stale
        `remaining` and collectively blowing through the window. The next
        response corrects the count with X's authoritative figure.
        """
        key = _key(account_id, endpoint_key)
        local = _local_state.get(key)
        if local is not None and local[0] is not None:
            _local_state[key] = (max(0, local[0] - 1), local[1])

        try:
            if await self.redis.exists(key):
                await self.redis.hincrby(key, "remaining", -1)  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001
            log.warning("x.ratelimit.consume_failed", error=str(exc), endpoint=endpoint_key)


def retry_after_seconds(headers: object, default: int = 60) -> int:
    """Seconds to wait after a 429, preferring X's own guidance."""
    get = getattr(headers, "get", None)
    if get is not None:
        raw = get("retry-after")
        if raw is not None:
            try:
                return max(1, int(raw))
            except (TypeError, ValueError):
                pass
        state = parse_headers(headers)
        if state.reset_at is not None:
            return max(1, state.seconds_until_reset + 1)
    return default


def backoff_seconds(attempt: int, base: float = 1.0, cap: float = 60.0) -> float:
    """Exponential backoff with full jitter.

    Full jitter rather than fixed doubling because collection jobs for several
    endpoints tend to fail together and would otherwise retry in lockstep.
    """
    import random

    return random.uniform(0, min(cap, base * (2**attempt)))  # noqa: S311
