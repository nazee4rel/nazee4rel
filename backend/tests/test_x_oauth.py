"""OAuth 2.0 + PKCE tests.

The PKCE maths and the single-use state handling are the parts where a subtle
bug is invisible in normal operation and only shows up as a security hole, so
they are pinned explicitly.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.x import endpoints as ep
from app.integrations.x.errors import XOAuthStateError
from app.integrations.x.oauth import (
    build_authorize_url,
    generate_pkce_pair,
    generate_state,
)
from app.models.usage import OAuthState
from app.models.user import User
from app.services.x_account_service import XAccountService


class TestPkce:
    def test_challenge_is_correct_s256_of_verifier(self) -> None:
        """The whole security property rests on this equality."""
        pair = generate_pkce_pair()
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(pair.verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode()
        )
        assert pair.challenge == expected
        assert pair.method == "S256"

    def test_verifier_length_within_rfc7636_bounds(self) -> None:
        assert 43 <= len(generate_pkce_pair().verifier) <= 128

    def test_no_base64_padding(self) -> None:
        """Padding characters are not permitted in the challenge."""
        pair = generate_pkce_pair()
        assert "=" not in pair.challenge
        assert "=" not in pair.verifier

    def test_pairs_are_unique(self) -> None:
        pairs = [generate_pkce_pair() for _ in range(100)]
        assert len({p.verifier for p in pairs}) == 100
        assert len({p.challenge for p in pairs}) == 100

    def test_states_are_unique_and_long(self) -> None:
        states = {generate_state() for _ in range(100)}
        assert len(states) == 100
        assert all(len(s) >= 32 for s in states)


class TestAuthorizeUrl:
    def test_contains_required_oauth_parameters(self) -> None:
        pair = generate_pkce_pair()
        url = build_authorize_url("state-123", pair.challenge, ep.BASE_SCOPES)
        query = parse_qs(urlparse(url).query)

        assert query["response_type"] == ["code"]
        assert query["state"] == ["state-123"]
        assert query["code_challenge"] == [pair.challenge]
        assert query["code_challenge_method"] == ["S256"]

    def test_never_leaks_the_verifier(self) -> None:
        """The verifier is the secret half and must not appear in the URL."""
        pair = generate_pkce_pair()
        url = build_authorize_url("state-123", pair.challenge, ep.BASE_SCOPES)
        assert pair.verifier not in url

    def test_scopes_are_space_delimited(self) -> None:
        url = build_authorize_url("s", "c", ("tweet.read", "users.read"))
        assert parse_qs(urlparse(url).query)["scope"] == ["tweet.read users.read"]


class TestScopePolicy:
    def test_offline_access_always_requested(self) -> None:
        """Without it X issues no refresh token and unattended collection dies."""
        assert "offline.access" in ep.requested_scopes(enable_write=False)
        assert "offline.access" in ep.requested_scopes(enable_write=True)

    def test_write_scope_absent_by_default(self) -> None:
        """Decision 4: posting must be impossible, not merely forbidden."""
        assert "tweet.write" not in ep.requested_scopes(enable_write=False)

    def test_write_scope_only_when_explicitly_enabled(self) -> None:
        assert "tweet.write" in ep.requested_scopes(enable_write=True)

    def test_irreversible_scopes_are_never_requested(self) -> None:
        """T3 actions stay technically impossible regardless of other failures."""
        for enabled in (False, True):
            scopes = set(ep.requested_scopes(enable_write=enabled))
            assert not scopes & {
                "follows.write",
                "like.write",
                "block.write",
                "mute.write",
                "tweet.moderate.write",
                "dm.read",
                "dm.write",
            }


class TestOAuthStateLifecycle:
    async def _user(self, db: AsyncSession) -> User:
        user = User(
            email="state@example.com",
            password_hash="x",
            display_name="State",
        )
        db.add(user)
        await db.flush()
        return user

    async def test_state_is_persisted_not_returned(self, db_session: AsyncSession) -> None:
        """The verifier lives server-side; only the challenge goes to X."""
        user = await self._user(db_session)
        url = await XAccountService(db_session).begin_authorization(user.id)

        row = await db_session.scalar(select(OAuthState).where(OAuthState.user_id == user.id))
        assert row is not None
        # Only the challenge is safe to put in the URL; the verifier is the
        # secret half and stays server-side.
        assert row.code_verifier not in url
        assert row.consumed_at is None

    async def test_replayed_callback_is_refused(self, db_session: AsyncSession) -> None:
        """A consumed state must never be redeemable a second time."""
        user = await self._user(db_session)
        pair = generate_pkce_pair()
        state = OAuthState(
            user_id=user.id,
            state="reused-state",
            code_verifier=pair.verifier,
            requested_scopes=[],
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            consumed_at=datetime.now(UTC),
        )
        db_session.add(state)
        await db_session.flush()

        with pytest.raises(XOAuthStateError):
            await XAccountService(db_session).complete_authorization("reused-state", "code")

    async def test_expired_state_is_refused(self, db_session: AsyncSession) -> None:
        user = await self._user(db_session)
        state = OAuthState(
            user_id=user.id,
            state="stale-state",
            code_verifier=generate_pkce_pair().verifier,
            requested_scopes=[],
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        db_session.add(state)
        await db_session.flush()

        with pytest.raises(XOAuthStateError):
            await XAccountService(db_session).complete_authorization("stale-state", "code")

    async def test_unknown_state_is_refused(self, db_session: AsyncSession) -> None:
        """An unrecognised state is a CSRF attempt, not a recoverable error."""
        with pytest.raises(XOAuthStateError):
            await XAccountService(db_session).complete_authorization("never-issued", "code")

    def test_is_usable_logic(self) -> None:
        now = datetime.now(UTC)
        fresh = OAuthState(
            user_id=None,  # type: ignore[arg-type]
            state="s",
            code_verifier="v",
            requested_scopes=[],
            expires_at=now + timedelta(minutes=5),
        )
        assert fresh.is_usable(now)

        fresh.consumed_at = now
        assert not fresh.is_usable(now)
