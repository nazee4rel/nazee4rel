"""Authentication endpoints.

The session cookie is httpOnly, SameSite=Lax and (in production) Secure. It is
never readable by JavaScript, which is the point: the browser holds a session
for *this* app, while X OAuth tokens stay server-side and never cross the
network boundary to the client at all.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import func, select

from app.api.deps import (
    SESSION_COOKIE_NAME,
    CurrentUser,
    CurrentUserAndSession,
    DbSession,
    rate_limit,
)
from app.core.config import get_settings
from app.core.errors import PermissionDeniedError
from app.models.user import User
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    MessageOut,
    RegisterRequest,
    SessionOut,
    UserOut,
)
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_session_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        domain=settings.cookie_domain or None,
        path="/",
    )


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit(limit=5, window_seconds=3600, scope="register"))],
)
async def register(
    payload: RegisterRequest, request: Request, response: Response, db: DbSession
) -> User:
    """Create an account.

    Single-tenant by decision 5: registration is open only until the first user
    exists, after which further accounts must be created by the owner. This
    avoids the classic mistake of leaving an open signup endpoint on a
    self-hosted instance that holds credentials to a real X account.
    """
    existing_count = await db.scalar(select(func.count()).select_from(User))
    if existing_count and existing_count > 0:
        raise PermissionDeniedError(
            "Registration is closed. The owner account already exists; "
            "additional users must be invited by the owner."
        )

    service = AuthService(db)
    user = await service.register(
        email=payload.email,
        password=payload.password,
        display_name=payload.display_name,
        timezone=payload.timezone,
        request=request,
    )
    token = await service.create_session(user, request)
    _set_session_cookie(response, token)
    return user


@router.post(
    "/login",
    response_model=UserOut,
    dependencies=[Depends(rate_limit(limit=10, window_seconds=900, scope="login"))],
)
async def login(payload: LoginRequest, request: Request, response: Response, db: DbSession) -> User:
    service = AuthService(db)
    user = await service.authenticate(payload.email, payload.password, request)
    token = await service.create_session(user, request)
    _set_session_cookie(response, token)
    return user


@router.post("/logout", response_model=MessageOut)
async def logout(
    pair: CurrentUserAndSession, request: Request, response: Response, db: DbSession
) -> MessageOut:
    user, session = pair
    await AuthService(db).revoke_session(session, user.id, request)
    response.delete_cookie(
        SESSION_COOKIE_NAME, path="/", domain=get_settings().cookie_domain or None
    )
    return MessageOut(message="Signed out.")


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> User:
    return user


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(pair: CurrentUserAndSession, db: DbSession) -> list[SessionOut]:
    user, current = pair
    sessions = await AuthService(db).list_sessions(user.id)
    return [
        SessionOut.model_validate(s).model_copy(update={"is_current": s.id == current.id})
        for s in sessions
    ]


@router.post("/sessions/revoke-others", response_model=MessageOut)
async def revoke_other_sessions(pair: CurrentUserAndSession, db: DbSession) -> MessageOut:
    user, current = pair
    count = await AuthService(db).revoke_all_sessions(user.id, except_session_id=current.id)
    return MessageOut(message=f"Revoked {count} other session(s).")


@router.post("/change-password", response_model=MessageOut)
async def change_password(
    payload: ChangePasswordRequest,
    pair: CurrentUserAndSession,
    request: Request,
    response: Response,
    db: DbSession,
) -> MessageOut:
    user, current = pair
    service = AuthService(db)
    await service.change_password(
        user,
        payload.current_password,
        payload.new_password,
        current_session_id=current.id,
        request=request,
    )
    return MessageOut(message="Password changed. Other sessions have been signed out.")
