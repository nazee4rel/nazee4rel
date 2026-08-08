"""OAuth 2.0 Authorization Code Flow with PKCE for X.

Why PKCE even though this is a confidential client with a secret: the code
challenge binds the authorization code to this specific handshake, so an
intercepted code cannot be redeemed by anyone else. X requires it regardless.

The one operational hazard worth understanding is **refresh token rotation**.
X refresh tokens are single-use: every exchange returns a new refresh token and
immediately invalidates the old one. Two workers refreshing concurrently means
one wins and the other permanently destroys the credential, forcing the user to
reconnect. Serialising refresh is therefore not an optimisation — it is what
keeps the connection alive. See `XAccountService.get_valid_access_token`.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.integrations.x.errors import XApiError, XTokenRefreshError

log = get_logger(__name__)

# RFC 7636 allows 43-128 characters; 96 random bytes lands comfortably inside.
_VERIFIER_BYTES = 72


@dataclass(frozen=True)
class PkcePair:
    verifier: str
    challenge: str
    method: str = "S256"


def generate_pkce_pair() -> PkcePair:
    """Create a PKCE verifier and its S256 challenge.

    The verifier is the secret half and must never leave the server — it is
    stored server-side against the state parameter, not in a cookie.
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(_VERIFIER_BYTES)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return PkcePair(verifier=verifier, challenge=challenge)


def generate_state() -> str:
    """Opaque CSRF token tying a callback back to the request that started it."""
    return secrets.token_urlsafe(32)


def build_authorize_url(state: str, challenge: str, scopes: tuple[str, ...]) -> str:
    settings = get_settings()
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.x_client_id,
            "redirect_uri": settings.x_redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{settings.x_authorize_url}?{query}"


@dataclass(frozen=True)
class TokenResponse:
    access_token: str
    refresh_token: str | None
    scopes: tuple[str, ...]
    expires_at: datetime | None

    @property
    def has_refresh_token(self) -> bool:
        return bool(self.refresh_token)


def _parse_token_response(payload: dict[str, object]) -> TokenResponse:
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise XApiError("X returned no access token.", payload=payload)

    refresh_token = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    expires_at = (
        datetime.now(UTC) + timedelta(seconds=int(expires_in))
        if isinstance(expires_in, (int, float))
        else None
    )

    # X returns granted scopes space-delimited. They may be narrower than what
    # we asked for if the user unticked something on the consent screen, so the
    # granted set — not the requested set — is what gets stored.
    raw_scope = payload.get("scope")
    scopes = tuple(raw_scope.split()) if isinstance(raw_scope, str) else ()

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token if isinstance(refresh_token, str) else None,
        scopes=scopes,
        expires_at=expires_at,
    )


async def _post_token(data: dict[str, str]) -> dict[str, object]:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            settings.x_token_url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            # Confidential client: credentials go in the Authorization header,
            # never in the body or a query string where they could be logged.
            auth=(settings.x_client_id, settings.x_client_secret),
        )

    if response.status_code != 200:
        # Deliberately does not include the request body in the message; it
        # contains the code or refresh token.
        raise XApiError(
            f"Token request failed ({response.status_code}).",
            status_code=response.status_code,
            payload=_safe_json(response),
        )

    return _safe_json(response) or {}


def _safe_json(response: httpx.Response) -> dict[str, object] | None:
    try:
        body = response.json()
        return body if isinstance(body, dict) else None
    except Exception:  # noqa: BLE001
        return None


async def exchange_code(code: str, verifier: str) -> TokenResponse:
    """Exchange an authorization code for tokens."""
    settings = get_settings()
    payload = await _post_token(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.x_redirect_uri,
            "code_verifier": verifier,
            "client_id": settings.x_client_id,
        }
    )
    tokens = _parse_token_response(payload)

    if not tokens.has_refresh_token:
        # Without offline.access there is no refresh token and the connection
        # dies in two hours. Better to fail loudly at connect time than to have
        # collection stop silently overnight.
        log.warning("x.oauth.no_refresh_token", scopes=list(tokens.scopes))

    return tokens


async def refresh_access_token(refresh_token: str) -> TokenResponse:
    """Exchange a refresh token for a new token pair.

    The supplied refresh token is invalidated by this call whether or not the
    caller manages to persist the replacement, so the new pair must be written
    to the database before anything else can fail.
    """
    settings = get_settings()
    try:
        payload = await _post_token(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": settings.x_client_id,
            }
        )
    except XApiError as exc:
        raise XTokenRefreshError(
            "Could not refresh the X access token. Because X refresh tokens are "
            "single-use, this connection cannot be repaired automatically — "
            "reconnect the account.",
            status_code=exc.status_code,
        ) from exc

    return _parse_token_response(payload)


async def revoke_token(token: str, token_type: str = "access_token") -> bool:  # noqa: S107
    """Best-effort revocation on disconnect.

    Returns success rather than raising: a token we are discarding anyway
    failing to revoke should not block the user from disconnecting.
    """
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                settings.x_revoke_url,
                data={
                    "token": token,
                    "token_type_hint": token_type,
                    "client_id": settings.x_client_id,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                auth=(settings.x_client_id, settings.x_client_secret),
            )
        return response.status_code == 200
    except Exception as exc:  # noqa: BLE001
        log.warning("x.oauth.revoke_failed", error=str(exc))
        return False
