"""API client, cost governor, rate limiter and probe tests.

Everything here runs against a mocked transport. That is deliberate beyond mere
speed: these tests must assert what happens on 403s, 429s and budget exhaustion,
and provoking those against the live API would cost money and risk the app's
standing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.x import endpoints as ep
from app.integrations.x.client import XApiClient, count_resources
from app.integrations.x.errors import (
    XBudgetExceededError,
    XCapabilityUnavailableError,
    XForbiddenError,
)
from app.integrations.x.probe import CapabilityProbe, _within_metrics_window
from app.integrations.x.ratelimit import (
    OutboundRateLimiter,
    RateLimitState,
    backoff_seconds,
    parse_headers,
    retry_after_seconds,
)
from app.models.enums import CapabilityStatus, XCapability
from app.models.usage import ApiUsageLedger
from app.models.user import User
from app.models.x_account import AccountCapability, XAccount
from app.services.cost_service import CostService


# --------------------------------------------------------------------- setup
async def make_account(db: AsyncSession) -> XAccount:
    user = User(email=f"u{uuid.uuid4().hex[:8]}@example.com", password_hash="x", display_name="U")
    db.add(user)
    await db.flush()
    account = XAccount(user_id=user.id, x_user_id="1234567890", username="testuser")
    db.add(account)
    await db.flush()
    return account


class StubLimiter(OutboundRateLimiter):
    """Bypasses Redis so client tests exercise the client, not the cache."""

    def __init__(self, allowed: bool = True, wait: int = 0) -> None:
        super().__init__(redis=None)
        self.allowed = allowed
        self.wait = wait

    async def check(self, account_id: str, endpoint_key: str) -> tuple[bool, int]:
        return self.allowed, self.wait

    async def record(self, *_a: object, **_k: object) -> None:
        return None

    async def consume(self, *_a: object, **_k: object) -> None:
        return None


def client_for(
    db: AsyncSession,
    account: XAccount,
    handler: object,
    *,
    scopes: tuple[str, ...] = (
        "tweet.read",
        "users.read",
        "follows.read",
        "like.read",
        "bookmark.read",
    ),
    limiter: OutboundRateLimiter | None = None,
) -> XApiClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return XApiClient(
        db,
        account,
        token_provider=lambda: _token(),
        granted_scopes=scopes,
        http_client=httpx.AsyncClient(transport=transport),
        limiter=limiter or StubLimiter(),
    )


async def _token() -> str:
    return "test-access-token"


def json_response(
    payload: dict[str, object], status: int = 200, headers: dict[str, str] | None = None
):
    return httpx.Response(status, json=payload, headers=headers or {})


# ---------------------------------------------------------------- registry
class TestEndpointRegistry:
    def test_unknown_endpoint_raises_before_any_io(self) -> None:
        """This is how 'never invent API endpoints' is enforced structurally."""
        with pytest.raises(ValueError, match="Unknown X endpoint"):
            ep.get_endpoint("users.followers_v3_beta")

    def test_every_endpoint_declares_a_cost_class(self) -> None:
        for endpoint in ep.REGISTRY.values():
            assert endpoint.cost_class in ep.CostClass

    def test_own_data_reads_are_owned_reads(self) -> None:
        """Owned Reads bill at a fifth of the general rate; misclassifying costs money."""
        for endpoint in ep.REGISTRY.values():
            if endpoint.method is ep.HttpMethod.GET:
                assert endpoint.cost_class is ep.CostClass.OWNED_READ

    def test_the_only_write_endpoint_is_post_creation(self) -> None:
        """Nothing in this system deletes or edits anything on X.

        The guarantee is structural: the client refuses any endpoint absent from
        the registry, so an absent delete endpoint is an unimplementable action
        rather than a discouraged one.
        """
        writes = [e for e in ep.REGISTRY.values() if e.method is not ep.HttpMethod.GET]
        assert [e.key for e in writes] == ["tweets.create"]
        assert ep.CREATE_TWEET.required_scopes == ("tweet.write", "tweet.read", "users.read")
        assert ep.CREATE_TWEET.capability is XCapability.WRITE_POSTS

    def test_uncertain_endpoint_is_flagged(self) -> None:
        """Sources conflict on the followers list, so it must not claim to be verified."""
        assert ep.USER_FOLLOWERS.verified is False
        assert ep.ME.verified is True


# -------------------------------------------------------------------- cost
class TestCostService:
    async def test_owned_read_rate_applies(self, db_session: AsyncSession) -> None:
        cost = CostService(db_session)
        # 100 resources at $0.001 = $0.10 = 100_000 micro-USD.
        assert cost.estimate_micros(ep.USER_TWEETS, 100) == 100_000

    async def test_legacy_subscription_is_not_metered(self, db_session: AsyncSession) -> None:
        cost = CostService(db_session)
        cost.settings = cost.settings.model_copy(update={"x_billing_mode": "legacy_subscription"})
        assert cost.estimate_micros(ep.USER_TWEETS, 10_000) == 0

    async def test_budget_blocks_when_exceeded(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)
        cost = CostService(db_session)
        cost.settings = cost.settings.model_copy(update={"x_monthly_budget_usd": 0.01})

        # $0.01 budget = 10_000 micros. Spend it.
        db_session.add(
            ApiUsageLedger(
                x_account_id=account.id,
                endpoint_key="users.tweets",
                method="GET",
                cost_class="OWNED_READ",
                resources_returned=10,
                estimated_cost_micros=10_000,
                billing_mode="pay_per_use",
            )
        )
        await db_session.flush()

        affordable, _estimate, spent = await cost.can_afford(ep.USER_TWEETS, 100, account.id)
        assert not affordable
        assert spent == 10_000

    async def test_blocked_calls_are_recorded_at_zero_cost(self, db_session: AsyncSession) -> None:
        """A blocked call must leave evidence — gaps in this dataset are permanent."""
        account = await make_account(db_session)
        cost = CostService(db_session)
        entry = await cost.record(
            ep.USER_TWEETS,
            x_account_id=account.id,
            resources_returned=100,
            was_blocked=True,
            block_reason="budget_exceeded",
        )
        assert entry.estimated_cost_micros == 0
        assert entry.was_blocked

    async def test_blocked_spend_excluded_from_totals(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)
        cost = CostService(db_session)
        await cost.record(ep.USER_TWEETS, x_account_id=account.id, resources_returned=50)
        await cost.record(
            ep.USER_TWEETS, x_account_id=account.id, resources_returned=50, was_blocked=True
        )
        assert await cost.spent_this_month(account.id) == 50_000

    async def test_budget_status_thresholds(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)
        cost = CostService(db_session)
        cost.settings = cost.settings.model_copy(update={"x_monthly_budget_usd": 1.00})

        assert (await cost.budget_status(account.id))["state"] == "healthy"

        await cost.record(ep.USER_TWEETS, x_account_id=account.id, resources_returned=950)
        assert (await cost.budget_status(account.id))["state"] == "critical"

        await cost.record(ep.USER_TWEETS, x_account_id=account.id, resources_returned=100)
        assert (await cost.budget_status(account.id))["state"] == "exhausted"

    def test_costs_are_integers(self, db_session: AsyncSession) -> None:
        """Money is never a float here."""
        cost = CostService(db_session)
        assert isinstance(cost.estimate_micros(ep.USER_TWEETS, 37), int)
        assert isinstance(cost.budget_micros, int)


# -------------------------------------------------------------- rate limits
class TestRateLimitParsing:
    def test_parses_x_headers(self) -> None:
        reset = int((datetime.now(UTC) + timedelta(minutes=10)).timestamp())
        state = parse_headers({"x-rate-limit-remaining": "42", "x-rate-limit-reset": str(reset)})
        assert state.remaining == 42
        assert state.reset_at is not None

    def test_missing_headers_mean_unknown_not_zero(self) -> None:
        """Absence is not exhaustion; assuming otherwise would stall collection."""
        state = parse_headers({})
        assert state.remaining is None
        assert not state.is_exhausted

    def test_exhausted_only_until_reset(self) -> None:
        past = datetime.now(UTC) - timedelta(minutes=1)
        future = datetime.now(UTC) + timedelta(minutes=5)
        assert not RateLimitState(0, past).is_exhausted
        assert RateLimitState(0, future).is_exhausted

    def test_retry_after_header_wins(self) -> None:
        assert retry_after_seconds({"retry-after": "120"}) == 120

    def test_retry_after_falls_back_to_reset(self) -> None:
        reset = int((datetime.now(UTC) + timedelta(seconds=30)).timestamp())
        assert 25 <= retry_after_seconds({"x-rate-limit-reset": str(reset)}) <= 35

    async def test_redis_outage_degrades_locally_instead_of_halting(self) -> None:
        """Regression: a Redis outage must not stop collection.

        This originally failed closed, which meant a cache outage silently
        halted every collection cycle. Because X drops non_public_metrics after
        30 days, a long enough halt destroys impression data permanently — a
        far worse outcome than the 429s that failing closed avoids.
        """

        class BrokenRedis:
            async def hgetall(self, *_a: object) -> dict[str, str]:
                raise ConnectionError("redis is down")

            async def exists(self, *_a: object) -> int:
                raise ConnectionError("redis is down")

        limiter = OutboundRateLimiter(redis=BrokenRedis())  # type: ignore[arg-type]
        allowed, wait = await limiter.check("acct-outage", "users.tweets")
        assert allowed is True
        assert wait == 0

    async def test_local_fallback_still_respects_a_known_exhaustion(self) -> None:
        """Degrading is not the same as ignoring limits."""

        class BrokenRedis:
            async def hgetall(self, *_a: object) -> dict[str, str]:
                raise ConnectionError("redis is down")

            def pipeline(self) -> object:  # sync, matching redis-py's signature
                raise ConnectionError("redis is down")

        limiter = OutboundRateLimiter(redis=BrokenRedis())  # type: ignore[arg-type]
        # X told us we were exhausted before Redis died; that must be remembered.
        await limiter.record(
            "acct-known",
            "users.tweets",
            RateLimitState(remaining=0, reset_at=datetime.now(UTC) + timedelta(minutes=5)),
        )
        allowed, wait = await limiter.check("acct-known", "users.tweets")
        assert allowed is False
        assert wait > 0

    def test_backoff_is_jittered_and_capped(self) -> None:
        values = [backoff_seconds(5, cap=10.0) for _ in range(50)]
        assert all(0 <= v <= 10.0 for v in values)
        assert len(set(values)) > 1  # jitter, not a fixed schedule


# ------------------------------------------------------------------ client
class TestClientBehaviour:
    async def test_successful_request_records_ledger(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)

        def handler(_request: httpx.Request) -> httpx.Response:
            return json_response(
                {"data": [{"id": "1"}, {"id": "2"}, {"id": "3"}]},
                headers={"x-rate-limit-remaining": "99"},
            )

        async with client_for(db_session, account, handler) as client:
            response = await client.request("users.tweets", path_params={"user_id": "1234567890"})

        assert response.resources_returned == 3
        entry = await db_session.scalar(select(ApiUsageLedger))
        assert entry is not None
        assert entry.resources_returned == 3
        # 3 resources at the owned-read rate.
        assert entry.estimated_cost_micros == 3_000
        assert entry.rate_limit_remaining == 99

    async def test_missing_scope_blocks_before_any_request(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)
        called = False

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return json_response({"data": []})

        async with client_for(db_session, account, handler, scopes=("users.read",)) as client:
            with pytest.raises(XCapabilityUnavailableError, match="missing scopes"):
                await client.request("users.followers", path_params={"user_id": "1"})

        assert not called, "a scope-blocked call must not reach the network"

    async def test_unavailable_capability_blocks_request(self, db_session: AsyncSession) -> None:
        """Never pay repeatedly for a call already known to be forbidden."""
        account = await make_account(db_session)
        db_session.add(
            AccountCapability(
                x_account_id=account.id,
                capability=XCapability.READ_FOLLOWERS_LIST,
                status=CapabilityStatus.FORBIDDEN,
            )
        )
        await db_session.flush()

        def handler(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("should not be called")

        async with client_for(db_session, account, handler) as client:
            with pytest.raises(XCapabilityUnavailableError):
                await client.request("users.followers", path_params={"user_id": "1"})

    async def test_unknown_capability_is_allowed_through(self, db_session: AsyncSession) -> None:
        """UNKNOWN means 'not yet checked' — the call is how we find out."""
        account = await make_account(db_session)
        db_session.add(
            AccountCapability(
                x_account_id=account.id,
                capability=XCapability.READ_OWN_POSTS,
                status=CapabilityStatus.UNKNOWN,
            )
        )
        await db_session.flush()

        def handler(_request: httpx.Request) -> httpx.Response:
            return json_response({"data": []})

        async with client_for(db_session, account, handler) as client:
            response = await client.request("users.tweets", path_params={"user_id": "1"})
        assert response.resources_returned == 0

    async def test_budget_exhaustion_blocks_and_logs(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)
        db_session.add(
            ApiUsageLedger(
                x_account_id=account.id,
                endpoint_key="users.tweets",
                method="GET",
                cost_class="OWNED_READ",
                resources_returned=1,
                estimated_cost_micros=25_000_000,  # the whole $25 default
                billing_mode="pay_per_use",
            )
        )
        await db_session.flush()

        def handler(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("should not reach the network")

        async with client_for(db_session, account, handler) as client:
            with pytest.raises(XBudgetExceededError):
                await client.request("users.tweets", path_params={"user_id": "1"})

        blocked = await db_session.scalar(
            select(func.count())
            .select_from(ApiUsageLedger)
            .where(ApiUsageLedger.was_blocked.is_(True))
        )
        assert blocked == 1

    async def test_403_raises_forbidden_and_is_not_retried(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)
        attempts = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return json_response({"detail": "Unsupported Authentication"}, status=403)

        async with client_for(db_session, account, handler) as client:
            with pytest.raises(XForbiddenError):
                await client.request("users.followers", path_params={"user_id": "1"})

        assert attempts == 1, "403 will not improve on retry"

    async def test_500_is_retried_then_succeeds(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)
        attempts = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return json_response({"detail": "oops"}, status=500)
            return json_response({"data": [{"id": "1"}]})

        async with client_for(db_session, account, handler) as client:
            response = await client.request("users.tweets", path_params={"user_id": "1"})

        assert attempts == 2
        assert response.resources_returned == 1

    async def test_partial_errors_surface_without_failing(self, db_session: AsyncSession) -> None:
        """X returns 200 with an errors array when a field is unavailable.

        Common for non_public_metrics on posts past the 30-day window. That is a
        data-availability fact, not a broken response.
        """
        account = await make_account(db_session)

        def handler(_request: httpx.Request) -> httpx.Response:
            return json_response(
                {
                    "data": [{"id": "1", "text": "hi"}],
                    "errors": [{"title": "Field unavailable", "field": "non_public_metrics"}],
                }
            )

        async with client_for(db_session, account, handler) as client:
            response = await client.request("users.tweets", path_params={"user_id": "1"})

        assert response.has_partial_errors
        assert response.resources_returned == 1

    async def test_bearer_token_sent_and_never_in_url(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization", "")
            seen["url"] = str(request.url)
            return json_response({"data": {"id": "1"}})

        async with client_for(db_session, account, handler) as client:
            await client.get_me()

        assert seen["auth"] == "Bearer test-access-token"
        assert "test-access-token" not in seen["url"]

    async def test_rate_limited_bucket_blocks_request(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)

        def handler(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("should not reach the network")

        from app.integrations.x.errors import XRateLimitError

        async with client_for(
            db_session, account, handler, limiter=StubLimiter(allowed=False, wait=42)
        ) as client:
            with pytest.raises(XRateLimitError):
                await client.request("users.tweets", path_params={"user_id": "1"})


class TestResourceCounting:
    def test_counts_list_and_object_and_none(self) -> None:
        """Billing is per resource returned, so this drives the whole cost model."""
        assert count_resources([{"id": "1"}, {"id": "2"}]) == 2
        assert count_resources({"id": "1"}) == 1
        assert count_resources(None) == 0
        assert count_resources([]) == 0


# ------------------------------------------------------------------- probe
class TestMetricsWindow:
    def test_recent_post_is_in_window(self) -> None:
        recent = (datetime.now(UTC) - timedelta(days=3)).isoformat().replace("+00:00", "Z")
        assert _within_metrics_window(recent)

    def test_old_post_is_outside_window(self) -> None:
        """Past 30 days X omits these fields regardless of access level."""
        old = (datetime.now(UTC) - timedelta(days=45)).isoformat().replace("+00:00", "Z")
        assert not _within_metrics_window(old)

    def test_malformed_timestamp_is_not_assumed_recent(self) -> None:
        assert not _within_metrics_window("not-a-date")
        assert not _within_metrics_window(None)


class TestProbeClassification:
    async def test_403_is_forbidden_not_error(self, db_session: AsyncSession) -> None:
        """FORBIDDEN is a settled answer; ERROR means ask again later."""
        account = await make_account(db_session)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/users/me"):
                return json_response(
                    {"data": {"id": "1", "username": "t", "public_metrics": {"followers_count": 5}}}
                )
            return json_response({"detail": "no"}, status=403)

        async with client_for(db_session, account, handler) as client:
            results = await CapabilityProbe(db_session, client).run()

        by_cap = {r.capability: r for r in results}
        assert by_cap[XCapability.READ_OWN_PROFILE].status is CapabilityStatus.AVAILABLE
        assert by_cap[XCapability.READ_FOLLOWERS_LIST].status is CapabilityStatus.FORBIDDEN

    async def test_empty_timeline_is_inconclusive_not_unavailable(
        self, db_session: AsyncSession
    ) -> None:
        """No posts is not evidence that impressions are unavailable."""
        account = await make_account(db_session)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/users/me"):
                return json_response(
                    {"data": {"id": "1", "username": "t", "public_metrics": {"followers_count": 5}}}
                )
            return json_response({"data": []})

        async with client_for(db_session, account, handler) as client:
            results = await CapabilityProbe(db_session, client).run()

        by_cap = {r.capability: r for r in results}
        assert by_cap[XCapability.READ_NON_PUBLIC_METRICS].status is CapabilityStatus.UNKNOWN

    async def test_recent_post_without_metrics_is_unavailable(
        self, db_session: AsyncSession
    ) -> None:
        """A recent post lacking the field IS evidence of unavailability."""
        account = await make_account(db_session)
        recent = (datetime.now(UTC) - timedelta(days=1)).isoformat().replace("+00:00", "Z")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/users/me"):
                return json_response(
                    {"data": {"id": "1", "username": "t", "public_metrics": {"followers_count": 5}}}
                )
            if "/tweets" in request.url.path:
                return json_response(
                    {
                        "data": [
                            {
                                "id": "1",
                                "created_at": recent,
                                "public_metrics": {"like_count": 3},
                            }
                        ]
                    }
                )
            return json_response({"data": []})

        async with client_for(db_session, account, handler) as client:
            results = await CapabilityProbe(db_session, client).run()

        by_cap = {r.capability: r for r in results}
        assert by_cap[XCapability.READ_PUBLIC_METRICS].status is CapabilityStatus.AVAILABLE
        assert by_cap[XCapability.READ_NON_PUBLIC_METRICS].status is CapabilityStatus.UNAVAILABLE

    async def test_old_posts_only_is_inconclusive(self, db_session: AsyncSession) -> None:
        """Every sampled post past 30 days cannot settle the question."""
        account = await make_account(db_session)
        old = (datetime.now(UTC) - timedelta(days=60)).isoformat().replace("+00:00", "Z")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/users/me"):
                return json_response(
                    {"data": {"id": "1", "username": "t", "public_metrics": {"followers_count": 5}}}
                )
            if "/tweets" in request.url.path:
                return json_response(
                    {"data": [{"id": "1", "created_at": old, "public_metrics": {"like_count": 1}}]}
                )
            return json_response({"data": []})

        async with client_for(db_session, account, handler) as client:
            results = await CapabilityProbe(db_session, client).run()

        by_cap = {r.capability: r for r in results}
        assert by_cap[XCapability.READ_NON_PUBLIC_METRICS].status is CapabilityStatus.UNKNOWN
        assert "30 days" in by_cap[XCapability.READ_NON_PUBLIC_METRICS].detail

    async def test_write_probe_never_posts(self, db_session: AsyncSession) -> None:
        """Probing write capability by posting would be an irreversible side effect."""
        account = await make_account(db_session)
        methods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            methods.append(request.method)
            if request.url.path.endswith("/users/me"):
                return json_response(
                    {"data": {"id": "1", "username": "t", "public_metrics": {"followers_count": 5}}}
                )
            return json_response({"data": []})

        async with client_for(db_session, account, handler) as client:
            results = await CapabilityProbe(db_session, client).run()

        assert all(m == "GET" for m in methods), "the probe must never write"
        by_cap = {r.capability: r for r in results}
        assert by_cap[XCapability.WRITE_POSTS].status is CapabilityStatus.UNAVAILABLE

    async def test_probe_persists_results(self, db_session: AsyncSession) -> None:
        account = await make_account(db_session)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/users/me"):
                return json_response(
                    {"data": {"id": "1", "username": "t", "public_metrics": {"followers_count": 5}}}
                )
            return json_response({"data": []})

        async with client_for(db_session, account, handler) as client:
            await CapabilityProbe(db_session, client).run()

        rows = list(
            await db_session.scalars(
                select(AccountCapability).where(AccountCapability.x_account_id == account.id)
            )
        )
        assert len(rows) == len(list(XCapability))
        assert all(r.last_checked_at is not None for r in rows)

    async def test_inconclusive_does_not_overwrite_settled_answer(
        self, db_session: AsyncSession
    ) -> None:
        """A later 'don't know' must not erase a previously confirmed answer."""
        account = await make_account(db_session)
        db_session.add(
            AccountCapability(
                x_account_id=account.id,
                capability=XCapability.READ_NON_PUBLIC_METRICS,
                status=CapabilityStatus.AVAILABLE,
            )
        )
        await db_session.flush()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/users/me"):
                return json_response(
                    {"data": {"id": "1", "username": "t", "public_metrics": {"followers_count": 5}}}
                )
            return json_response({"data": []})  # empty timeline => inconclusive

        async with client_for(db_session, account, handler) as client:
            await CapabilityProbe(db_session, client).run()

        row = await db_session.scalar(
            select(AccountCapability).where(
                AccountCapability.x_account_id == account.id,
                AccountCapability.capability == XCapability.READ_NON_PUBLIC_METRICS,
            )
        )
        assert row is not None
        assert row.status is CapabilityStatus.AVAILABLE
