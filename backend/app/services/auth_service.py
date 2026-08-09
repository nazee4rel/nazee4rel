"""Registration, login, session lifecycle."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AuthenticationError, ConflictError, SessionExpiredError
from app.core.logging import get_logger
from app.core.security import (
    generate_session_token,
    hash_password,
    hash_session_token,
    password_needs_rehash,
    verify_password,
)
from app.models.enums import AuditAction, UserRole
from app.models.user import Session, User
from app.services.audit_service import AuditService, client_ip, user_agent

log = get_logger(__name__)

# A valid Argon2 hash of a random string. Verified against when the email is
# unknown so that login latency does not reveal whether an account exists.
_DUMMY_HASH = hash_password("timing-equalisation-placeholder-value")


class AuthService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.audit = AuditService(db)
        self.settings = get_settings()

    # ------------------------------------------------------------- registration
    async def register(
        self,
        email: str,
        password: str,
        display_name: str,
        timezone: str = "UTC",
        role: UserRole = UserRole.OWNER,
        request: Request | None = None,
    ) -> User:
        email = email.lower().strip()

        existing = await self.db.scalar(select(User).where(User.email == email))
        if existing is not None:
            raise ConflictError("An account with that email already exists.")

        user = User(
            email=email,
            password_hash=hash_password(password),
            display_name=display_name.strip(),
            timezone=timezone,
            role=role,
        )
        self.db.add(user)
        await self.db.flush()

        await self.audit.record(
            AuditAction.USER_CREATED, user_id=user.id, request=request, email=email
        )
        return user

    # -------------------------------------------------------------------- login
    async def authenticate(self, email: str, password: str, request: Request | None = None) -> User:
        email = email.lower().strip()
        user = await self.db.scalar(
            select(User).where(User.email == email, User.deleted_at.is_(None))
        )

        if user is None:
            # Spend the same time as a real verification so response timing
            # cannot be used to enumerate accounts.
            verify_password(password, _DUMMY_HASH)
            await self.audit.record(AuditAction.LOGIN_FAILED, request=request, email=email)
            raise AuthenticationError()

        if not verify_password(password, user.password_hash):
            await self.audit.record(
                AuditAction.LOGIN_FAILED, user_id=user.id, request=request, email=email
            )
            raise AuthenticationError()

        if not user.is_active:
            await self.audit.record(
                AuditAction.LOGIN_FAILED,
                user_id=user.id,
                request=request,
                reason="inactive",
            )
            raise AuthenticationError("This account has been deactivated.")

        # Transparently upgrade the hash if Argon2 parameters have changed.
        if password_needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)

        user.last_login_at = datetime.now(UTC)
        await self.audit.record(AuditAction.LOGIN_SUCCEEDED, user_id=user.id, request=request)
        return user

    # ----------------------------------------------------------------- sessions
    async def create_session(self, user: User, request: Request | None = None) -> str:
        """Create a session and return the raw token (only time it exists)."""
        token = generate_session_token()
        session = Session(
            user_id=user.id,
            token_hash=hash_session_token(token),
            expires_at=datetime.now(UTC) + timedelta(hours=self.settings.session_ttl_hours),
            ip_address=client_ip(request),
            user_agent=user_agent(request),
        )
        self.db.add(session)
        await self.db.flush()
        return token

    async def resolve_session(self, token: str) -> tuple[User, Session]:
        """Look up an active session by raw token."""
        session = await self.db.scalar(
            select(Session).where(Session.token_hash == hash_session_token(token))
        )
        if session is None:
            raise SessionExpiredError()

        now = datetime.now(UTC)
        expires_at = session.expires_at
        # SQLite round-trips naive datetimes; normalise before comparing.
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)

        if session.revoked_at is not None or expires_at <= now:
            raise SessionExpiredError()

        user = await self.db.get(User, session.user_id)
        if user is None or not user.is_active or user.deleted_at is not None:
            raise SessionExpiredError()

        return user, session

    async def revoke_session(
        self, session: Session, user_id: uuid.UUID, request: Request | None = None
    ) -> None:
        session.revoked_at = datetime.now(UTC)
        await self.audit.record(AuditAction.LOGOUT, user_id=user_id, request=request)

    async def revoke_all_sessions(
        self, user_id: uuid.UUID, except_session_id: uuid.UUID | None = None
    ) -> int:
        stmt = select(Session).where(Session.user_id == user_id, Session.revoked_at.is_(None))
        sessions = list(await self.db.scalars(stmt))
        now = datetime.now(UTC)
        count = 0
        for s in sessions:
            if except_session_id is not None and s.id == except_session_id:
                continue
            s.revoked_at = now
            count += 1
        await self.audit.record(AuditAction.SESSION_REVOKED, user_id=user_id, revoked_count=count)
        return count

    async def list_sessions(self, user_id: uuid.UUID) -> list[Session]:
        return list(
            await self.db.scalars(
                select(Session)
                .where(Session.user_id == user_id, Session.revoked_at.is_(None))
                .order_by(Session.created_at.desc())
            )
        )

    # ----------------------------------------------------------------- password
    async def change_password(
        self,
        user: User,
        current_password: str,
        new_password: str,
        current_session_id: uuid.UUID | None = None,
        request: Request | None = None,
    ) -> None:
        if not verify_password(current_password, user.password_hash):
            raise AuthenticationError("Current password is incorrect.")

        user.password_hash = hash_password(new_password)
        await self.audit.record(AuditAction.PASSWORD_CHANGED, user_id=user.id, request=request)
        # A password change should end any session an attacker might hold.
        await self.revoke_all_sessions(user.id, except_session_id=current_session_id)
