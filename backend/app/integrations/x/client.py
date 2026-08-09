"""The X API client.

Every outbound call passes through `request()`, which applies, in order:

1. **Endpoint registry check** — an undeclared endpoint raises before any I/O,
   which is how "never invent API endpoints" is enforced structurally.
2. **Scope check** — against what the user actually granted, not what we asked
   for. A half-approved consent screen degrades gracefully instead of producing
   a wall of 403s.
3. **Capability check** — against probe results, so we do not repeatedly pay for
   calls already known to be forbidden.
4. **Cost pre-authorisation** — estimated high, refused if it would breach the
   monthly budget.
5. **Rate limiting** — primed from X's own response headers.
6. **Request, with retry** — exponential backoff with full jitter on 429/5xx.
7. **Ledger write** — always, including for calls that were blocked, so gaps in
   collection are explainable rather than mysterious.

Ordering is deliberate: the free local checks run before the ones that cost
money or time.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.core.config import get_settings
from app.core.logging import get_logger
from app.integrations.x import endpoints as ep
from app.integrations.x.errors import (
    XApiError,
    XAuthError,
    XBudgetExceededError,
    XCapabilityUnavailableError,
    XForbiddenError,
    XNotFoundError,
    XRateLimitError,
    XServerError,
)
from app.integrations.x.ratelimit import (
    OutboundRateLimiter,
    backoff_seconds,
    parse_headers,
    retry_after_seconds,
)
from app.models.enums import CapabilityStatus
from app.models.x_account import AccountCapability, XAccount
from app.services.cost_service import CostService

log = get_logger(__name__)

TokenProvider = Callable[[], Awaitable[str]]

MAX_RETRIES = 3
REQUEST_TIMEOUT = 30.0


@dataclass
class XResponse:
    """A successful X API response, plus what it cost us."""

    data: Any
    includes: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    resources_returned: int = 0
    status_code: int = 200

    @property
    def items(self) -> list[dict[str, Any]]:
        """`data` normalised to a list, since X returns an object or an array."""
        if self.data is None:
            return []
        return self.data if isinstance(self.data, list) else [self.data]

    @property
    def next_token(self) -> str | None:
        token = self.meta.get("next_token")
        return token if isinstance(token, str) else None

    @property
    def has_partial_errors(self) -> bool:
        """X returns 200 with an `errors` array for partially-fulfilled requests.

        Common and important here: asking for `non_public_metrics` on a post
        older than 30 days succeeds overall while reporting that field as
        unavailable. That is a data-availability fact, not a failure — the
        collector records it rather than treating the response as broken.
        """
        return bool(self.errors)


def count_resources(data: Any) -> int:
    """Resources billed by this response.

    X bills per resource returned, not per request, so this is the quantity
    that drives cost — never the number of calls.
    """
    if data is None:
        return 0
    if isinstance(data, list):
        return len(data)
    return 1


class XApiClient:
    def __init__(
        self,
        db: AsyncSession,
        x_account: XAccount,
        token_provider: TokenProvider,
        *,
        granted_scopes: tuple[str, ...] = (),
        http_client: httpx.AsyncClient | None = None,
        limiter: OutboundRateLimiter | None = None,
    ) -> None:
        self.db = db
        self.account = x_account
        self.token_provider = token_provider
        self.granted_scopes = set(granted_scopes)
        self.settings = get_settings()
        self.cost = CostService(db)
        self.limiter = limiter or OutboundRateLimiter()
        self._http = http_client
        self._owns_http = http_client is None

    async def __aenter__(self) -> XApiClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
        return self._http

    # ------------------------------------------------------------ pre-checks
    def _check_scopes(self, endpoint: ep.Endpoint) -> str | None:
        if not self.granted_scopes:
            return None  # scopes unknown; let X be the authority
        missing = set(endpoint.required_scopes) - self.granted_scopes
        return f"missing scopes: {', '.join(sorted(missing))}" if missing else None

    async def _check_capability(self, endpoint: ep.Endpoint) -> str | None:
        if endpoint.capability is None:
            return None
        row = await self.db.scalar(
            select(AccountCapability).where(
                AccountCapability.x_account_id == self.account.id,
                AccountCapability.capability == endpoint.capability,
            )
        )
        # UNKNOWN is permitted through: not yet checked is not the same as
        # confirmed absent, and the call is how we find out.
        if row is not None and row.status in (
            CapabilityStatus.UNAVAILABLE,
            CapabilityStatus.FORBIDDEN,
        ):
            return f"capability {endpoint.capability.value} is {row.status.value}"
        return None

    # -------------------------------------------------------------- request
    async def request(
        self,
        endpoint_key: str,
        *,
        path_params: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        expected_resources: int = 1,
        bypass_capability_check: bool = False,
    ) -> XResponse:
        endpoint = ep.get_endpoint(endpoint_key)
        account_id = str(self.account.id)

        # 1-3. Free local checks first.
        for reason in (
            self._check_scopes(endpoint),
            None if bypass_capability_check else await self._check_capability(endpoint),
        ):
            if reason:
                await self.cost.record(
                    endpoint,
                    x_account_id=self.account.id,
                    was_blocked=True,
                    block_reason=reason[:64],
                )
                raise XCapabilityUnavailableError(reason, endpoint=endpoint.key)

        # 4. Budget.
        affordable, estimate, spent = await self.cost.can_afford(
            endpoint, expected_resources, self.account.id
        )
        if not affordable:
            await self.cost.record(
                endpoint,
                x_account_id=self.account.id,
                was_blocked=True,
                block_reason="budget_exceeded",
                estimate_micros=estimate,
            )
            raise XBudgetExceededError(
                "This request would exceed the configured X API monthly budget. "
                "Raise X_MONTHLY_BUDGET_USD or wait for the next billing period.",
                spent_micros=spent,
                budget_micros=self.cost.budget_micros,
            )

        # 5. Rate limit.
        allowed, wait = await self.limiter.check(account_id, endpoint.key)
        if not allowed:
            await self.cost.record(
                endpoint,
                x_account_id=self.account.id,
                was_blocked=True,
                block_reason="rate_limited",
                wait_seconds=wait,
            )
            raise XRateLimitError(
                f"Rate limit for {endpoint.key} is exhausted; resets in {wait}s.",
                retry_after=wait,
                endpoint=endpoint.key,
            )

        return await self._send_with_retry(endpoint, path_params or {}, params or {}, json_body)

    async def _send_with_retry(
        self,
        endpoint: ep.Endpoint,
        path_params: dict[str, str],
        params: dict[str, Any],
        json_body: dict[str, Any] | None = None,
    ) -> XResponse:
        url = endpoint.url(self.settings.x_api_base_url, **path_params)
        account_id = str(self.account.id)
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES):
            token = await self.token_provider()
            await self.limiter.consume(account_id, endpoint.key)

            started = time.monotonic()
            try:
                response = await self.http.request(
                    endpoint.method.value,
                    url,
                    params=params or None,
                    json=json_body,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "User-Agent": f"x-account-intelligence-agent/{__version__}",
                    },
                )
            except httpx.HTTPError as exc:
                last_error = exc
                await self.cost.record(
                    endpoint,
                    x_account_id=self.account.id,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    error=f"transport: {exc}",
                    attempt=attempt,
                )
                if attempt == MAX_RETRIES - 1:
                    raise XApiError(
                        f"Network error calling X: {exc}", endpoint=endpoint.key
                    ) from exc
                await self._sleep(backoff_seconds(attempt))
                continue

            latency_ms = int((time.monotonic() - started) * 1000)
            rl = parse_headers(response.headers)
            await self.limiter.record(account_id, endpoint.key, rl)

            # --- success -------------------------------------------------
            # 201 as well as 200: creating a post returns Created.
            if response.status_code in (200, 201):
                body = self._json(response)
                data = body.get("data")
                resources = count_resources(data)
                await self.cost.record(
                    endpoint,
                    x_account_id=self.account.id,
                    resources_returned=resources,
                    status_code=response.status_code,
                    latency_ms=latency_ms,
                    rate_limit_remaining=rl.remaining,
                    rate_limit_reset_at=rl.reset_at,
                )
                errors = body.get("errors")
                return XResponse(
                    data=data,
                    includes=body.get("includes") or {},
                    meta=body.get("meta") or {},
                    errors=errors if isinstance(errors, list) else [],
                    resources_returned=resources,
                    status_code=response.status_code,
                )

            # --- failure -------------------------------------------------
            await self.cost.record(
                endpoint,
                x_account_id=self.account.id,
                status_code=response.status_code,
                latency_ms=latency_ms,
                rate_limit_remaining=rl.remaining,
                rate_limit_reset_at=rl.reset_at,
                error=response.text[:500],
                attempt=attempt,
            )

            if response.status_code == 429:
                wait = retry_after_seconds(response.headers)
                if attempt == MAX_RETRIES - 1:
                    raise XRateLimitError(
                        "X rate limit hit.", retry_after=wait, endpoint=endpoint.key
                    )
                await self._sleep(min(wait, 60))
                continue

            if response.status_code >= 500:
                last_error = XServerError(
                    f"X server error ({response.status_code}).",
                    status_code=response.status_code,
                    endpoint=endpoint.key,
                )
                if attempt == MAX_RETRIES - 1:
                    raise last_error
                await self._sleep(backoff_seconds(attempt))
                continue

            # 4xx other than 429 will not improve on retry.
            raise self._client_error(response, endpoint)

        raise XApiError(
            f"Request to {endpoint.key} failed after {MAX_RETRIES} attempts.",
            endpoint=endpoint.key,
        ) from last_error

    # --------------------------------------------------------------- helpers
    @staticmethod
    async def _sleep(seconds: float) -> None:
        import asyncio

        await asyncio.sleep(seconds)

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
            return body if isinstance(body, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _client_error(self, response: httpx.Response, endpoint: ep.Endpoint) -> XApiError:
        body = self._json(response)
        detail = body.get("detail") or body.get("title") or response.text[:200]
        status = response.status_code

        message, error_type = {
            401: (f"X rejected the access token: {detail}", XAuthError),
            403: (
                f"X forbids this request at your current access level: {detail}",
                XForbiddenError,
            ),
            404: (f"Not found: {detail}", XNotFoundError),
        }.get(status, (f"X request failed ({status}): {detail}", XApiError))

        return error_type(message, status_code=status, endpoint=endpoint.key, payload=body)

    # ------------------------------------------------------- typed wrappers
    async def get_me(self) -> XResponse:
        return await self.request(
            ep.ME.key,
            params={"user.fields": ",".join(ep.USER_FIELDS)},
            expected_resources=1,
        )

    async def get_own_posts(
        self,
        *,
        max_results: int = 100,
        pagination_token: str | None = None,
        include_private_metrics: bool = True,
        since_id: str | None = None,
    ) -> XResponse:
        """Fetch own posts.

        `include_private_metrics` adds the fields that carry impressions. They
        are returned only for posts under 30 days old; older posts simply omit
        them, which is why the day-29 snapshot in Phase 4 is mandatory.
        """
        fields = list(ep.PUBLIC_TWEET_FIELDS)
        if include_private_metrics:
            fields += list(ep.PRIVATE_TWEET_FIELDS)

        params: dict[str, Any] = {
            "max_results": max_results,
            "tweet.fields": ",".join(fields),
        }
        if pagination_token:
            params["pagination_token"] = pagination_token
        if since_id:
            params["since_id"] = since_id

        return await self.request(
            ep.USER_TWEETS.key,
            path_params={"user_id": self.account.x_user_id},
            params=params,
            expected_resources=max_results,
        )
