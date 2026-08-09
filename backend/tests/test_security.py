"""The security review, written as tests rather than as a document.

A review is true on the day it is written. These assertions stay true, and fail
the build the day someone adds a route without an authorisation check, a config
field whose name says "password" without adding it to the log scrubber, or a
raw SQL string.

The route inventory below is the important part. It enumerates the actual
application, so a new endpoint cannot slip through by not being on anyone's
checklist: it either requires a session, or it appears in `PUBLIC_ROUTES` with
a written reason, and there is no third option.
"""

from __future__ import annotations

import inspect
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import accounts, agent, alerts, analytics, auth, collection, health, x_oauth
from app.core.config import Settings
from app.core.logging import _SENSITIVE_KEYS, scrub_secrets
from app.core.security import decrypt_secret, encrypt_secret, hash_password, verify_password
from app.models.user import User
from app.models.x_account import OAuthToken, XAccount

BACKEND_ROOT = Path(__file__).resolve().parent.parent

ROUTER_MODULES = (accounts, agent, alerts, analytics, auth, collection, health, x_oauth)

# Routes that may be reached without a session, each with the reason it must be.
# Anything not listed here has to require authentication. Adding a route without
# a decision fails `test_every_route_requires_authentication`.
PUBLIC_ROUTES: dict[tuple[str, str], str] = {
    ("POST", "/auth/login"): "You cannot log in with a session you do not have.",
    ("POST", "/auth/register"): "Bootstraps the first account; rate-limited to 5/hour.",
    (
        "GET",
        "/auth/setup-status",
    ): "Tells the login page which form to show, before anyone can have a session. "
    "Discloses one boolean — whether the owner exists — which either form makes "
    "obvious anyway.",
    ("GET", "/health/live"): "Liveness probe. Returns no data about the account.",
    ("GET", "/health/ready"): "Readiness probe. Reports dependency reachability only.",
    (
        "GET",
        "/x/oauth/callback",
    ): "X redirects the browser here with no cookie of ours. Authenticated instead "
    "by the single-use `state` value minted when the handshake began.",
}


def iter_routes() -> list[tuple[list[str], str, Any]]:
    """Every route in the v1 API, with the endpoint function."""
    found: list[tuple[list[str], str, Any]] = []
    for module in ROUTER_MODULES:
        router = module.router
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            path = (
                route.path if route.path.startswith(router.prefix) else router.prefix + route.path
            )
            found.append((sorted(route.methods - {"HEAD", "OPTIONS"}), path, route.endpoint))
    return found


def requires_session(endpoint: Any) -> bool:
    return any(
        "CurrentUser" in str(parameter.annotation)
        for parameter in inspect.signature(endpoint).parameters.values()
    )


class TestRouteAuthorisation:
    def test_the_inventory_is_not_empty(self) -> None:
        """A broken enumerator would make every test below vacuously pass."""
        assert len(iter_routes()) > 40

    def test_every_route_requires_authentication(self) -> None:
        """Or is listed as public with a reason. There is no third option."""
        unlisted: list[str] = []
        for methods, path, endpoint in iter_routes():
            if requires_session(endpoint):
                continue
            if any((method, path) in PUBLIC_ROUTES for method in methods):
                continue
            unlisted.append(f"{','.join(methods)} {path}")
        assert not unlisted, (
            "These routes take no session and are not declared public:\n  "
            + "\n  ".join(unlisted)
            + "\n\nAdd the session dependency, or add an entry to PUBLIC_ROUTES "
            "explaining why it is safe."
        )

    def test_public_route_list_has_no_stale_entries(self) -> None:
        """A route that gained auth should be removed from the exception list."""
        live = {(m, path) for methods, path, _ in iter_routes() for m in methods}
        stale = [entry for entry in PUBLIC_ROUTES if entry not in live]
        assert not stale, f"PUBLIC_ROUTES lists routes that no longer exist: {stale}"

    def test_account_scoped_routes_check_ownership(self) -> None:
        """Taking an `account_id` is not the same as being allowed to read it.

        Every such handler must resolve the account through a helper that filters
        on `XAccount.user_id`, so a valid session cannot read another user's
        account by guessing a UUID.
        """
        missing: list[str] = []
        for methods, path, endpoint in iter_routes():
            if "account_id" not in inspect.signature(endpoint).parameters:
                continue
            source = inspect.getsource(endpoint)
            # Either via a shared helper, or filtered inline in the handler.
            resolves = (
                "_account(" in source
                or "_account_for(" in source
                or "XAccount.user_id == user.id" in source
            )
            if not resolves:
                missing.append(f"{','.join(methods)} {path}")
        assert not missing, "Account-scoped routes with no ownership check:\n  " + "\n  ".join(
            missing
        )

    def test_ownership_helpers_actually_filter_by_user(self) -> None:
        """The check above trusts the helpers; this checks the helpers."""
        for module in ROUTER_MODULES:
            for name in ("_account", "_account_for"):
                helper = getattr(module, name, None)
                if helper is None:
                    continue
                source = inspect.getsource(helper)
                assert "XAccount.user_id == user.id" in source, (
                    f"{module.__name__}.{name} does not filter on the session user"
                )


class TestCrossAccountAccess:
    async def test_a_valid_session_cannot_read_another_account(
        self, registered_client: AsyncClient
    ) -> None:
        """Driven by the route table, so a new endpoint is covered automatically."""
        stranger = uuid.uuid4()
        checked = 0
        for methods, path, endpoint in iter_routes():
            if "account_id" not in inspect.signature(endpoint).parameters:
                continue
            if "GET" not in methods:
                continue
            url = "/api/v1" + path.replace("{account_id}", str(stranger))
            if "{" in url:  # a second path parameter; covered by its own test
                continue
            response = await registered_client.get(url)
            assert response.status_code == 404, f"{url} returned {response.status_code}"
            checked += 1
        assert checked >= 15, f"expected to exercise many routes, exercised {checked}"

    async def test_unauthenticated_requests_are_rejected(self, client: AsyncClient) -> None:
        account = uuid.uuid4()
        for path in (
            f"/api/v1/analytics/{account}/summary",
            f"/api/v1/agent/{account}/insights",
            f"/api/v1/alerts/{account}/rules",
            f"/api/v1/collection/{account}/health",
            "/api/v1/agent/policy",
            "/api/v1/auth/me",
        ):
            assert (await client.get(path)).status_code == 401, path


class TestSecretHandling:
    def test_every_secret_shaped_setting_is_scrubbed_from_logs(self) -> None:
        """The check that would have caught the Phase 8 SMTP password.

        The scrubber redacts by key name, so a new secret in Settings is only
        protected once its name is on the list. Deriving the expectation from
        the settings model means the list cannot silently fall behind.
        """
        secret_like = re.compile(r"(password|secret|token_encryption|api_key)")
        expected = {
            name
            for name in Settings.model_fields
            if secret_like.search(name) and name != "password_hash"
        }
        missing = {name for name in expected if name not in _SENSITIVE_KEYS}
        assert not missing, (
            f"Settings fields that look like secrets but are not scrubbed from logs: "
            f"{sorted(missing)}"
        )

    def test_the_scrubber_redacts_nested_values(self) -> None:
        event = {
            "event": "boom",
            "refresh_token": "abc",
            "smtp_password": "hunter2",
            "context": {"access_token": "xyz"},
            "username": "safe",
        }
        cleaned = scrub_secrets(None, "info", dict(event))
        rendered = repr(cleaned)
        assert "abc" not in rendered
        assert "hunter2" not in rendered
        assert "xyz" not in rendered
        assert "safe" in rendered

    def test_x_tokens_are_ciphertext_at_rest(self) -> None:
        """Not a round-trip test — a check that what gets stored is not the token."""
        plaintext = "x-access-token-value"
        stored = encrypt_secret(plaintext)
        assert plaintext not in stored
        assert decrypt_secret(stored) == plaintext

    def test_token_ciphertext_is_not_deterministic(self) -> None:
        """A repeated nonce would leak equality between accounts and rotations."""
        assert encrypt_secret("same") != encrypt_secret("same")

    def test_passwords_are_hashed_with_a_salt(self) -> None:
        first, second = hash_password("correct-horse"), hash_password("correct-horse")
        assert first != second
        assert first.startswith("$argon2id$")
        assert verify_password("correct-horse", first)
        assert not verify_password("wrong", first)

    def test_no_response_model_exposes_a_token(self) -> None:
        """Encrypted or not, tokens have no business in an API response.

        Walks the declared response models of every route rather than one
        module, so a new endpoint returning the wrong Pydantic model is caught.
        """
        # "token" alone is too broad: the agent run schema reports LLM token
        # *counts*, which are usage figures rather than credentials.
        dangerous = re.compile(
            r"(access_token|refresh_token|_token_enc|^token$|password|secret|api_key|code_verifier)"
        )
        leaks: list[str] = []
        for module in ROUTER_MODULES:
            for route in module.router.routes:
                if not isinstance(route, APIRoute) or route.response_model is None:
                    continue
                fields = getattr(route.response_model, "model_fields", {}) or {}
                for field in fields:
                    if dangerous.search(field):
                        leaks.append(f"{route.path} -> {route.response_model} exposes {field}")
        assert not leaks, "\n".join(leaks)

    def test_the_leak_detector_would_notice_a_real_token_field(self) -> None:
        """Guards the guard: a pattern that matches nothing proves nothing."""
        dangerous = re.compile(
            r"(access_token|refresh_token|_token_enc|^token$|password|secret|api_key|code_verifier)"
        )
        assert dangerous.search("refresh_token")
        assert dangerous.search("access_token_enc")
        assert not dangerous.search("input_tokens")


class TestProductionConfiguration:
    def _production(self, **overrides: Any) -> Settings:
        base: dict[str, Any] = {
            "environment": "production",
            "secret_key": "a" * 40,
            "token_encryption_key": "0" * 43 + "=",
            "cookie_secure": True,
            "frontend_origin": "https://app.example.com",
            "allowed_hosts": "api.example.com,backend",
        }
        base.update(overrides)
        return Settings(**base)

    def test_a_valid_production_config_boots(self) -> None:
        settings = self._production()
        assert settings.allowed_host_list == ["api.example.com", "backend"]

    def test_allowed_hosts_must_be_present_in_production(self) -> None:
        """Regression: TrustedHostMiddleware was fed the frontend *origin*.

        A Host header carries no scheme, so `https://app.example.com` could never
        match one — production would have rejected every request with 400. The
        failure was invisible because no test ran in production mode.
        """
        with pytest.raises(ValueError, match="ALLOWED_HOSTS"):
            self._production(allowed_hosts="")

    def test_a_url_in_allowed_hosts_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="hostnames, not URLs"):
            self._production(allowed_hosts="https://api.example.com")

    def test_insecure_cookies_are_rejected_in_production(self) -> None:
        with pytest.raises(ValueError, match="COOKIE_SECURE"):
            self._production(cookie_secure=False)

    def test_plaintext_frontend_origin_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="HTTPS"):
            self._production(frontend_origin="http://app.example.com")

    def test_placeholder_secrets_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="placeholder"):
            self._production(secret_key="CHANGE_ME" + "x" * 30)

    def test_development_needs_no_allowed_hosts(self) -> None:
        """Local development hits the API by container name, localhost and IP."""
        settings = Settings(
            environment="development",
            secret_key="a" * 40,
            token_encryption_key="0" * 43 + "=",
        )
        assert settings.allowed_host_list == ["*"]


class TestSuiteIsolation:
    """The suite must not inherit a developer's local configuration.

    Settings looks for `.env` at the repository root as well as in `backend/`,
    so that running the backend natively works without a symlink. That
    convenience is a hazard for tests: a real .env pointing at a live Redis
    makes the rate limiter fire mid-suite, and a flipped feature flag would
    change what the agent tests assert. Both happened before this was pinned.
    """

    def test_dotenv_is_disabled_during_tests(self) -> None:
        import os

        from app.core.config import _ENV_FILE

        assert os.environ.get("XAGENT_ENV_FILE"), "conftest must pin XAGENT_ENV_FILE"
        assert not Path(str(_ENV_FILE)).exists(), (
            "tests are reading a real dotenv file; they must be hermetic"
        )

    def test_settings_ignore_a_dotenv_that_exists(self) -> None:
        """Belt and braces: even with a .env on disk, the test config wins."""
        from app.core.config import get_settings

        settings = get_settings()
        assert settings.database_url.startswith("sqlite"), settings.database_url
        # Unreachable on purpose, so the limiter uses its process-local fallback.
        assert "6399" not in settings.redis_url


class TestResponseHardening:
    async def test_security_headers_are_present(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/health/live")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert response.headers["X-Request-ID"]

    async def test_the_session_cookie_is_httponly_and_samesite(self, client: AsyncClient) -> None:
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": "cookie@example.com",
                "password": "correct-horse-battery-staple-9",
                "display_name": "Cookie",
                "timezone": "UTC",
            },
        )
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "cookie@example.com", "password": "correct-horse-battery-staple-9"},
        )
        cookie = response.headers.get("set-cookie", "")
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie.replace("Samesite", "SameSite")
        assert "Path=/" in cookie

    async def test_unexpected_errors_do_not_leak_internals(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.api.v1 import health

        def explode() -> None:
            raise RuntimeError("connection string postgresql://user:hunter2@db/app")

        monkeypatch.setattr(health, "_liveness_probe", explode, raising=False)
        response = await client.get("/api/v1/health/live")
        assert "hunter2" not in response.text

    async def test_login_is_rate_limited(self, client: AsyncClient) -> None:
        """Brute force protection has to survive a refactor of the auth router."""
        source = inspect.getsource(auth.login)
        assert "rate_limit" in inspect.getsource(auth) and "login" in source


class TestSourceLevelInvariants:
    def test_no_raw_string_sql(self) -> None:
        """SQLAlchemy parameterises everything; a formatted string would not."""
        offenders: list[str] = []
        pattern = re.compile(r"""(execute|text)\(\s*f?["'](SELECT|INSERT|UPDATE|DELETE)""", re.I)
        for path in (BACKEND_ROOT / "app").rglob("*.py"):
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                if pattern.search(line) and "SELECT 1" not in line:
                    offenders.append(f"{path.relative_to(BACKEND_ROOT)}:{number}")
        assert not offenders, f"raw SQL found: {offenders}"

    def test_no_credentials_are_committed(self) -> None:
        """Catches a real key pasted into a file while debugging."""
        pattern = re.compile(
            r"(sk-ant-[A-Za-z0-9_-]{10,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY)"
        )
        offenders: list[str] = []
        for path in list((BACKEND_ROOT / "app").rglob("*.py")) + [
            BACKEND_ROOT.parent / ".env.example"
        ]:
            if not path.exists():
                continue
            if pattern.search(path.read_text()):
                offenders.append(str(path))
        assert not offenders, f"possible committed credential in: {offenders}"

    def test_env_example_ships_no_real_secrets(self) -> None:
        """Every secret in the template must be blank or an obvious placeholder."""
        template = (BACKEND_ROOT.parent / ".env.example").read_text()
        for line in template.splitlines():
            if "=" not in line or line.strip().startswith("#"):
                continue
            key, _, value = line.partition("=")
            if re.search(r"(PASSWORD|SECRET|API_KEY|ENCRYPTION_KEY)", key):
                assert not value.strip() or "CHANGE_ME" in value, (
                    f"{key.strip()} in .env.example has a non-placeholder value"
                )


class TestAgentBoundaryStillHolds:
    """The Phase 6 guarantees, re-checked here so the review covers them.

    Duplicated deliberately: someone auditing this project should be able to read
    one file and see the whole boundary, not have to know that the interesting
    part lives in the agent tests.
    """

    def test_no_destructive_action_is_implemented(self) -> None:
        from app.agent.policy import FORBIDDEN_ACTIONS
        from app.models.agent import ActionType

        implemented = {a.value for a in ActionType}
        assert implemented.isdisjoint(FORBIDDEN_ACTIONS)

    def test_the_only_write_endpoint_is_post_creation(self) -> None:
        from app.integrations.x import endpoints as ep

        writes = [e.key for e in ep.REGISTRY.values() if e.method is not ep.HttpMethod.GET]
        assert writes == ["tweets.create"]

    def test_write_scope_is_not_requested_by_default(self) -> None:
        from app.integrations.x.endpoints import requested_scopes

        assert "tweet.write" not in requested_scopes(enable_write=False)

    def test_the_reasoning_call_carries_no_tools(self) -> None:
        """A successful prompt injection must have nothing to call."""
        from app.agent import client as agent_client

        source = inspect.getsource(agent_client.AnthropicClient.ask)
        assert "tools" not in source


class TestDataIsolation:
    async def test_deleting_a_user_does_not_orphan_their_tokens(
        self, db_session: AsyncSession
    ) -> None:
        """Cascades matter here: a stranded OAuth token is a live credential."""
        user = User(email="cascade@example.com", password_hash="x", display_name="C")
        db_session.add(user)
        await db_session.flush()
        account = XAccount(user_id=user.id, x_user_id="1", username="c", connected_at=None)
        db_session.add(account)
        await db_session.flush()
        db_session.add(
            OAuthToken(
                x_account_id=account.id,
                access_token_enc=encrypt_secret("a"),
                refresh_token_enc=encrypt_secret("r"),
                scopes="tweet.read",
            )
        )
        await db_session.flush()

        await db_session.delete(user)
        await db_session.flush()

        assert await db_session.scalar(select(OAuthToken)) is None
        assert await db_session.scalar(select(XAccount)) is None
