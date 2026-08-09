"""Application users and their sessions.

This project never stores an X/Twitter password — the OAuth 2.0 + PKCE flow
means we never receive one. The password here is for logging into *this*
dashboard only.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import UserRole

if TYPE_CHECKING:
    from app.models.x_account import XAccount


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)

    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role"), default=UserRole.OWNER, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # IANA timezone. Posting-time analysis is meaningless without it, so it is
    # captured at registration rather than defaulted silently.
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)

    # Optional TOTP second factor (enrolment lands in Phase 9).
    totp_secret_enc: Mapped[str | None] = mapped_column(String(512), default=None)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    sessions: Mapped[list[Session]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    x_accounts: Mapped[list[XAccount]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_users_active", "is_active", "deleted_at"),)

    def __repr__(self) -> str:
        return f"<User {self.email} role={self.role.value}>"


class Session(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Server-side session.

    Only the SHA-256 digest of the token is stored, so a database leak does not
    yield usable sessions. Server-side storage (rather than a stateless JWT) is
    what makes immediate revocation possible — necessary because this app holds
    credentials that can post to a real account.
    """

    __tablename__ = "sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # Recorded for the audit trail and for "sign out everywhere else".
    ip_address: Mapped[str | None] = mapped_column(String(45), default=None)  # IPv6-sized
    user_agent: Mapped[str | None] = mapped_column(String(512), default=None)

    user: Mapped[User] = relationship(back_populates="sessions")

    __table_args__ = (Index("ix_sessions_user_active", "user_id", "revoked_at", "expires_at"),)

    def is_valid(self, now: datetime) -> bool:
        return self.revoked_at is None and self.expires_at > now
