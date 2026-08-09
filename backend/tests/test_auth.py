"""Authentication endpoint tests."""

from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import TEST_EMAIL, TEST_PASSWORD


class TestSetupStatus:
    """Which form the login page shows.

    This was inferred from a `POST /auth/register` probe, where 403 meant "owner
    exists" and everything else meant "registration is open". The endpoint is
    rate-limited to 5/hour, and the probe spent that budget itself — so the
    sixth login page load in an hour got a 429, read it as "open", and offered
    to create an owner on an instance that already had one. The owner was then
    unable to sign in at all.
    """

    async def test_reports_no_owner_on_a_fresh_instance(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/auth/setup-status")
        assert resp.status_code == 200
        assert resp.json() == {"owner_exists": False}

    async def test_reports_an_owner_once_one_exists(self, registered_client: AsyncClient) -> None:
        resp = await registered_client.get("/api/v1/auth/setup-status")
        assert resp.status_code == 200
        assert resp.json() == {"owner_exists": True}

    async def test_needs_no_session(self, client: AsyncClient) -> None:
        """It is the question you ask *before* you can have one."""
        assert (await client.get("/api/v1/auth/setup-status")).status_code == 200

    async def test_repeated_calls_do_not_exhaust_anything(self, client: AsyncClient) -> None:
        """The specific regression: the page load must not spend a mutation budget."""
        for _ in range(12):
            resp = await client.get("/api/v1/auth/setup-status")
            assert resp.status_code == 200, resp.text
        # And registration is still available afterwards, which is what the old
        # probe destroyed.
        resp = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "owner@example.com",
                "password": TEST_PASSWORD,
                "display_name": "Owner",
            },
        )
        assert resp.status_code == 201

    async def test_discloses_nothing_beyond_the_one_boolean(
        self, registered_client: AsyncClient
    ) -> None:
        resp = await registered_client.get("/api/v1/auth/setup-status")
        assert set(resp.json()) == {"owner_exists"}
        assert TEST_EMAIL not in resp.text


class TestRegistration:
    async def test_creates_owner_and_sets_cookie(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "owner@example.com",
                "password": TEST_PASSWORD,
                "display_name": "Owner",
                "timezone": "Africa/Lagos",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["email"] == "owner@example.com"
        assert body["role"] == "OWNER"
        assert body["timezone"] == "Africa/Lagos"
        assert "xagent_session" in resp.cookies

    async def test_never_returns_password_material(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "owner@example.com",
                "password": TEST_PASSWORD,
                "display_name": "Owner",
            },
        )
        assert "password" not in resp.text.lower()
        assert "hash" not in resp.text.lower()

    async def test_second_registration_is_refused(self, registered_client: AsyncClient) -> None:
        """Single-tenant by decision 5: signup closes after the owner exists."""
        resp = await registered_client.post(
            "/api/v1/auth/register",
            json={
                "email": "intruder@example.com",
                "password": TEST_PASSWORD,
                "display_name": "Intruder",
            },
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "permission_denied"

    async def test_rejects_short_password(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/auth/register",
            json={"email": "a@example.com", "password": "short", "display_name": "A"},
        )
        assert resp.status_code == 422

    async def test_rejects_letters_only_password(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "a@example.com",
                "password": "onlylettershereplease",
                "display_name": "A",
            },
        )
        assert resp.status_code == 422

    async def test_rejects_unknown_timezone(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "a@example.com",
                "password": TEST_PASSWORD,
                "display_name": "A",
                "timezone": "Mars/Olympus_Mons",
            },
        )
        assert resp.status_code == 422


class TestLogin:
    async def test_succeeds_with_correct_credentials(self, registered_client: AsyncClient) -> None:
        registered_client.cookies.clear()
        resp = await registered_client.post(
            "/api/v1/auth/login", json={"email": TEST_EMAIL, "password": TEST_PASSWORD}
        )
        assert resp.status_code == 200
        assert "xagent_session" in resp.cookies

    async def test_email_is_case_insensitive(self, registered_client: AsyncClient) -> None:
        registered_client.cookies.clear()
        resp = await registered_client.post(
            "/api/v1/auth/login",
            json={"email": TEST_EMAIL.upper(), "password": TEST_PASSWORD},
        )
        assert resp.status_code == 200

    async def test_wrong_password_rejected(self, registered_client: AsyncClient) -> None:
        registered_client.cookies.clear()
        resp = await registered_client.post(
            "/api/v1/auth/login",
            json={"email": TEST_EMAIL, "password": "definitely-wrong-password"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "authentication_failed"

    async def test_unknown_user_gives_identical_error(self, registered_client: AsyncClient) -> None:
        """No account enumeration: unknown email and wrong password look alike."""
        registered_client.cookies.clear()
        unknown = await registered_client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": TEST_PASSWORD},
        )
        wrong = await registered_client.post(
            "/api/v1/auth/login",
            json={"email": TEST_EMAIL, "password": "definitely-wrong-password"},
        )
        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json() == wrong.json()


class TestSessionLifecycle:
    async def test_me_requires_authentication(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/auth/me")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "session_expired"

    async def test_me_returns_current_user(self, registered_client: AsyncClient) -> None:
        resp = await registered_client.get("/api/v1/auth/me")
        assert resp.status_code == 200
        assert resp.json()["email"] == TEST_EMAIL

    async def test_logout_invalidates_session(self, registered_client: AsyncClient) -> None:
        assert (await registered_client.post("/api/v1/auth/logout")).status_code == 200
        assert (await registered_client.get("/api/v1/auth/me")).status_code == 401

    async def test_forged_cookie_rejected(self, client: AsyncClient) -> None:
        client.cookies.set("xagent_session", "totally-made-up-token-value")
        assert (await client.get("/api/v1/auth/me")).status_code == 401

    async def test_lists_active_sessions(self, registered_client: AsyncClient) -> None:
        resp = await registered_client.get("/api/v1/auth/sessions")
        assert resp.status_code == 200
        sessions = resp.json()
        assert len(sessions) == 1
        assert sessions[0]["is_current"] is True


class TestChangePassword:
    async def test_changes_password_and_revokes_others(
        self, registered_client: AsyncClient
    ) -> None:
        new_password = "an-entirely-new-password-42"
        resp = await registered_client.post(
            "/api/v1/auth/change-password",
            json={"current_password": TEST_PASSWORD, "new_password": new_password},
        )
        assert resp.status_code == 200

        # Current session survives; the old password no longer works.
        assert (await registered_client.get("/api/v1/auth/me")).status_code == 200

        registered_client.cookies.clear()
        old = await registered_client.post(
            "/api/v1/auth/login", json={"email": TEST_EMAIL, "password": TEST_PASSWORD}
        )
        assert old.status_code == 401

        new = await registered_client.post(
            "/api/v1/auth/login", json={"email": TEST_EMAIL, "password": new_password}
        )
        assert new.status_code == 200

    async def test_wrong_current_password_rejected(self, registered_client: AsyncClient) -> None:
        resp = await registered_client.post(
            "/api/v1/auth/change-password",
            json={
                "current_password": "not-the-current-password",
                "new_password": "a-brand-new-password-42",
            },
        )
        assert resp.status_code == 401
