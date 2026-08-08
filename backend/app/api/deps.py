"""Shared FastAPI dependencies: session auth, RBAC, rate limiting."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Cookie, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import PermissionDeniedError, RateLimitError, SessionExpiredError
from app.core.ratelimit import check_rate_limit
from app.db.session import get_db
from app.models.enums import UserRole
from app.models.user import Session, User
from app.services.audit_service import client_ip
from app.services.auth_service import AuthService

SESSION_COOKIE_NAME = "xagent_session"

DbSession = Annotated[AsyncSession, Depends(get_db)]


async def get_current_user_and_session(
    db: DbSession,
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
) -> tuple[User, Session]:
    if not session_token:
        raise SessionExpiredError("Not authenticated.")
    return await AuthService(db).resolve_session(session_token)


async def get_current_user(
    pair: Annotated[tuple[User, Session], Depends(get_current_user_and_session)],
) -> User:
    return pair[0]


CurrentUser = Annotated[User, Depends(get_current_user)]
CurrentUserAndSession = Annotated[tuple[User, Session], Depends(get_current_user_and_session)]


def require_role(*allowed: UserRole) -> Callable[..., Coroutine[Any, Any, User]]:
    """RBAC guard.

    OWNER implicitly satisfies every requirement — there is exactly one owner in
    a single-tenant deployment and locking them out of their own instance would
    be an unhelpful kind of strictness.
    """

    async def _guard(user: CurrentUser) -> User:
        if user.role is UserRole.OWNER or user.role in allowed:
            return user
        raise PermissionDeniedError(
            f"This action requires one of: {', '.join(r.value for r in allowed)}."
        )

    return _guard


def rate_limit(
    limit: int | None = None, window_seconds: int = 60, scope: str = "global"
) -> Callable[..., Coroutine[Any, Any, None]]:
    """Per-IP rate limit for an endpoint or router."""

    async def _limiter(request: Request) -> None:
        settings = get_settings()
        effective = limit if limit is not None else settings.api_requests_per_minute
        ip = client_ip(request) or "unknown"
        allowed, reset_in = await check_rate_limit(f"{scope}:{ip}", effective, window_seconds)
        if not allowed:
            raise RateLimitError(retry_after=reset_in)

    return _limiter
