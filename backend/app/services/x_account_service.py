"""Connecting, disconnecting and authenticating X accounts.

The delicate part is `get_valid_access_token`. X refresh tokens are single-use
and rotate: every exchange returns a new refresh token and invalidates the old
one. So two workers refreshing the same account concurrently is not a race that
merely wastes a call — the loser destroys the credential and the user has to
reconnect. Refresh is therefore serialised behind a distributed lock, with a
re-read after acquiring it so the second worker uses the first one's result
instead of burning the token again.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.ratelimit import get_redis
from app.core.security import decrypt_secret, encrypt_secret
from app.integrations.x import oauth
from app.integrations.x.endpoints import requested_scopes
from app.integrations.x.errors import XOAuthStateError, XTokenRefreshError
from app.models.enums import AuditAction
from app.models.usage import OAuthState
from app.models.x_account import OAuthToken, XAccount
from app.services.audit_service import AuditService

log = get_logger(__name__)

# Refresh this far ahead of expiry so a token cannot lapse mid-request.
REFRESH_MARGIN = timedelta(minutes=10)

_LOCK_TTL_SECONDS = 30
_LOCK_WAIT_SECONDS = 20


class XAccountService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.settings = get_settings()
        self.audit = AuditService(db)

    # ----------------------------------------------------------- handshake
    async def begin_authorization(
        self, user_id: uuid.UUID, redirect_after: str | None = None
    ) -> str:
        """Start the OAuth handshake and return the URL to send the user to."""
        if not self.settings.x_client_id or not self.settings.x_client_secret:
            raise XOAuthStateError(
                "X_CLIENT_ID and X_CLIENT_SECRET are not configured. Create an app "
                "in the X developer portal with OAuth 2.0 enabled as a confidential "
                "client, then set them in .env."
            )

        pkce = oauth.generate_pkce_pair()
        state = oauth.generate_state()
        scopes = requested_scopes(self.settings.x_enable_write_actions)

        self.db.add(
            OAuthState(
                user_id=user_id,
                state=state,
                code_verifier=pkce.verifier,
                requested_scopes=list(scopes),
                expires_at=datetime.now(UTC)
                + timedelta(seconds=self.settings.x_oauth_state_ttl_seconds),
                redirect_after=redirect_after,
            )
        )
        await self.db.flush()

        log.info(
            "x.oauth.started",
            user_id=str(user_id),
            scopes=list(scopes),
            write_enabled=self.settings.x_enable_write_actions,
        )
        return oauth.build_authorize_url(state, pkce.challenge, scopes)

    async def complete_authorization(
        self, state: str, code: str, request: Request | None = None
    ) -> XAccount:
        """Handle the OAuth callback: verify state, exchange code, store tokens."""
        now = datetime.now(UTC)
        row = await self.db.scalar(select(OAuthState).where(OAuthState.state == state))

        if row is None or not row.is_usable(now):
            # Unknown, replayed or expired — all three are refused rather than
            # repaired, since each is either CSRF or a duplicate callback.
            raise XOAuthStateError(
                "This authorization link is invalid or has already been used. "
                "Start the connection again."
            )

        # Burn the state before the exchange, so a duplicated callback cannot
        # redeem the same code twice.
        row.consumed_at = now
        await self.db.flush()

        tokens = await oauth.exchange_code(code, row.code_verifier)

        me = await _fetch_me(tokens.access_token)

        x_user_id = str(me.get("id") or "")
        username = str(me.get("username") or "")
        if not x_user_id:
            raise XOAuthStateError("X did not return a user id for the authorized account.")

        account = await self.db.scalar(
            select(XAccount)
            .where(XAccount.user_id == row.user_id, XAccount.x_user_id == x_user_id)
            .options(selectinload(XAccount.oauth_token))
        )

        if account is None:
            account = XAccount(
                user_id=row.user_id,
                x_user_id=x_user_id,
                username=username,
                display_name=_as_str(me.get("name")),
                profile_image_url=_as_str(me.get("profile_image_url")),
                connected_at=now,
                # Marks the start of the impression dataset. Nothing before this
                # can ever be backfilled.
                collection_started_at=now,
            )
            self.db.add(account)
            await self.db.flush()
        else:
            account.username = username
            account.display_name = _as_str(me.get("name"))
            account.profile_image_url = _as_str(me.get("profile_image_url"))
            account.is_active = True
            account.disconnected_at = None
            if account.connected_at is None:
                account.connected_at = now

        await self._store_tokens(account, tokens)
        await self.audit.record(
            AuditAction.X_ACCOUNT_CONNECTED,
            user_id=row.user_id,
            x_account_id=account.id,
            request=request,
            username=username,
            granted_scopes=list(tokens.scopes),
        )

        log.info(
            "x.oauth.connected",
            username=username,
            granted_scopes=list(tokens.scopes),
            has_refresh_token=tokens.has_refresh_token,
        )
        return account

    async def _store_tokens(self, account: XAccount, tokens: oauth.TokenResponse) -> None:
        existing = await self.db.scalar(
            select(OAuthToken).where(OAuthToken.x_account_id == account.id)
        )
        if existing is None:
            existing = OAuthToken(x_account_id=account.id)
            self.db.add(existing)

        existing.access_token_enc = encrypt_secret(tokens.access_token)
        if tokens.refresh_token:
            existing.refresh_token_enc = encrypt_secret(tokens.refresh_token)
        existing.scopes = list(tokens.scopes)
        existing.expires_at = tokens.expires_at
        existing.last_refreshed_at = datetime.now(UTC)
        existing.refresh_failure_count = 0
        await self.db.flush()

    # --------------------------------------------------------------- tokens
    async def get_valid_access_token(self, account: XAccount) -> str:
        """Return a usable access token, refreshing under a lock if needed."""
        token_row = await self._load_token(account.id)

        if not self._needs_refresh(token_row):
            return decrypt_secret(token_row.access_token_enc)

        if not token_row.refresh_token_enc:
            raise XTokenRefreshError(
                "The access token has expired and no refresh token is stored "
                "(the offline.access scope was not granted). Reconnect the account."
            )

        async with _refresh_lock(account.id) as acquired:
            if not acquired:
                # Someone else is refreshing. Wait briefly, then use their result.
                await asyncio.sleep(1.0)
                await self.db.refresh(token_row)
                if not self._needs_refresh(token_row):
                    return decrypt_secret(token_row.access_token_enc)

            # Re-read inside the lock: another worker may have refreshed between
            # our check and our acquiring it. Refreshing again would consume a
            # token that is already spent.
            await self.db.refresh(token_row)
            if not self._needs_refresh(token_row):
                return decrypt_secret(token_row.access_token_enc)

            return await self._do_refresh(account, token_row)

    async def _do_refresh(self, account: XAccount, token_row: OAuthToken) -> str:
        refresh_token = decrypt_secret(token_row.refresh_token_enc or "")
        try:
            tokens = await oauth.refresh_access_token(refresh_token)
        except XTokenRefreshError:
            token_row.refresh_failure_count += 1
            await self.db.flush()
            await self.db.commit()
            await self.audit.system(
                "ERROR",
                "x_oauth",
                "Refresh failed; the account must be reconnected.",
                x_account_id=account.id,
                failure_count=token_row.refresh_failure_count,
            )
            raise

        # X has now invalidated the old refresh token, so the replacement must
        # be durable before anything else is allowed to fail. Commit here rather
        # than deferring to the request's normal commit.
        await self._store_tokens(account, tokens)
        await self.db.commit()

        await self.audit.record(
            AuditAction.X_TOKEN_REFRESHED, x_account_id=account.id, user_id=account.user_id
        )
        log.info("x.oauth.refreshed", account=account.username)
        return tokens.access_token

    async def _load_token(self, account_id: uuid.UUID) -> OAuthToken:
        row = await self.db.scalar(select(OAuthToken).where(OAuthToken.x_account_id == account_id))
        if row is None:
            raise XTokenRefreshError("No X credentials stored. Connect the account first.")
        return row

    @staticmethod
    def _needs_refresh(token_row: OAuthToken) -> bool:
        if token_row.expires_at is None:
            return False  # no expiry known; let a 401 drive the refresh
        expires = token_row.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return expires - REFRESH_MARGIN <= datetime.now(UTC)

    async def granted_scopes(self, account_id: uuid.UUID) -> tuple[str, ...]:
        row = await self.db.scalar(select(OAuthToken).where(OAuthToken.x_account_id == account_id))
        return tuple(row.scopes) if row else ()

    # ----------------------------------------------------------- disconnect
    async def disconnect(
        self, account: XAccount, request: Request | None = None, revoke: bool = True
    ) -> None:
        """Disconnect an account.

        Credentials are destroyed; collected history is not. Impressions older
        than 30 days cannot be re-fetched from X at any price, so deleting the
        snapshots would be irreversible in a way disconnecting is not.
        """
        token_row = await self.db.scalar(
            select(OAuthToken).where(OAuthToken.x_account_id == account.id)
        )

        if token_row is not None:
            if revoke:
                with contextlib.suppress(Exception):
                    await oauth.revoke_token(decrypt_secret(token_row.access_token_enc))
            await self.db.delete(token_row)

        account.is_active = False
        account.disconnected_at = datetime.now(UTC)
        await self.db.flush()

        await self.audit.record(
            AuditAction.X_ACCOUNT_DISCONNECTED,
            user_id=account.user_id,
            x_account_id=account.id,
            request=request,
            username=account.username,
        )


# --------------------------------------------------------------------- utils
def _as_str(value: object) -> str | None:
    """Narrow an untyped JSON value to an optional string."""
    return value if isinstance(value, str) else None


async def _fetch_me(access_token: str) -> dict[str, object]:
    """Fetch the authorizing user's profile during connection.

    Runs outside `XApiClient` because at this point no XAccount row exists yet
    to carry capabilities, budget scoping or a ledger.
    """
    import httpx

    settings = get_settings()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{settings.x_api_base_url.rstrip('/')}/2/users/me",
            params={"user.fields": "id,name,username,profile_image_url,public_metrics"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if response.status_code != 200:
        raise XOAuthStateError(
            f"Could not read the authorized profile from X ({response.status_code})."
        )
    body = response.json()
    data = body.get("data")
    return data if isinstance(data, dict) else {}


@contextlib.asynccontextmanager
async def _refresh_lock(account_id: uuid.UUID) -> AsyncIterator[bool]:
    """Distributed lock serialising token refresh for one account.

    Falls back to proceeding without the lock if Redis is unavailable: a
    single-worker deployment still works, and blocking all collection on a cache
    outage would be worse than the small risk of a concurrent refresh.
    """
    key = f"xrefreshlock:{account_id}"
    token = secrets.token_hex(16)
    redis = None
    acquired = False

    try:
        redis = get_redis()
        deadline = asyncio.get_event_loop().time() + _LOCK_WAIT_SECONDS
        while asyncio.get_event_loop().time() < deadline:
            if await redis.set(key, token, nx=True, ex=_LOCK_TTL_SECONDS):
                acquired = True
                break
            await asyncio.sleep(0.25)
    except Exception as exc:  # noqa: BLE001
        log.warning("x.oauth.lock_unavailable", error=str(exc))
        yield True
        return

    try:
        yield acquired
    finally:
        if acquired and redis is not None:
            with contextlib.suppress(Exception):
                # Release only if we still hold it; a lock that expired and was
                # taken by someone else must not be deleted.
                current = await redis.get(key)
                if current == token:
                    await redis.delete(key)
