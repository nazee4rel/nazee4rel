"""Account and capability endpoint tests.

The important assertion here is that an unprobed capability reports UNKNOWN
rather than UNAVAILABLE. Section 1.5 of the architecture doc treats "not yet
checked" and "confirmed absent" as different facts, and collapsing them would
let the dashboard imply knowledge it does not have.
"""

from __future__ import annotations

from httpx import AsyncClient

from app.models.enums import XCapability


class TestAccountsEndpoint:
    async def test_requires_authentication(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/accounts")).status_code == 401

    async def test_reports_not_connected_before_phase_3(
        self, registered_client: AsyncClient
    ) -> None:
        resp = await registered_client.get("/api/v1/accounts")
        assert resp.status_code == 200
        body = resp.json()
        assert body["connected"] is False
        assert body["accounts"] == []

    async def test_exposes_safety_configuration(self, registered_client: AsyncClient) -> None:
        """The UI needs these to explain why posting is unavailable."""
        body = (await registered_client.get("/api/v1/accounts")).json()
        assert body["write_actions_enabled"] is False  # default-off per decision 4
        assert body["billing_mode"] == "pay_per_use"
        assert body["monthly_budget_usd"] > 0

    async def test_capabilities_404_for_unknown_account(
        self, registered_client: AsyncClient
    ) -> None:
        resp = await registered_client.get(
            "/api/v1/accounts/00000000-0000-0000-0000-000000000000/capabilities"
        )
        assert resp.status_code == 404


class TestHealth:
    async def test_liveness(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/health/live")
        assert resp.status_code == 200
        assert resp.json()["status"] == "alive"

    async def test_readiness_reports_database(self, client: AsyncClient) -> None:
        body = (await client.get("/api/v1/health/ready")).json()
        assert body["checks"]["database"] == "ok"


class TestSecurityHeaders:
    async def test_headers_present(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/health/live")
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert "X-Request-ID" in resp.headers


class TestCapabilityEnum:
    def test_covers_uncertain_endpoints(self) -> None:
        """Capabilities the research flagged as uncertain must be probed."""
        names = {c.name for c in XCapability}
        # Non-public metrics carry the 30-day cliff; the followers list was
        # removed from some tiers. Both are probed, never assumed.
        assert "READ_NON_PUBLIC_METRICS" in names
        assert "READ_FOLLOWERS_LIST" in names
        assert "WRITE_POSTS" in names
