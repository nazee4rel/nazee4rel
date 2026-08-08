"""Connected X/Twitter accounts, their OAuth tokens, and their capability matrix."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import CapabilityStatus, XCapability

if TYPE_CHECKING:
    from app.models.user import User

# JSONB on Postgres, plain JSON on SQLite so the test suite can run without a
# database container.
JSONType = JSON().with_variant(JSONB(), "postgresql")


class XAccount(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "x_accounts"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # The numeric X user id is the real identity: handles change, ids do not.
    # Every join and every historical record keys off this, never the username.
    x_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), default=None)
    profile_image_url: Mapped[str | None] = mapped_column(String(512), default=None)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # Marks the start of our impression dataset. Because non-public metrics are
    # only available for posts under 30 days old, nothing before this timestamp
    # can ever be backfilled — the dashboard renders it as an explicit boundary
    # rather than letting the chart imply zero.
    collection_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    user: Mapped[User] = relationship(back_populates="x_accounts")
    oauth_token: Mapped[OAuthToken | None] = relationship(
        back_populates="x_account", cascade="all, delete-orphan", uselist=False
    )
    capabilities: Mapped[list[AccountCapability]] = relationship(
        back_populates="x_account", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("user_id", "x_user_id", name="uq_x_accounts_user_xuser"),
        Index("ix_x_accounts_active", "user_id", "is_active"),
    )

    def __repr__(self) -> str:
        return f"<XAccount @{self.username} id={self.x_user_id}>"


class OAuthToken(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """X OAuth 2.0 tokens, encrypted at rest with AES-256-GCM.

    Kept in a separate table from `x_accounts` for two reasons: the account row
    is read constantly by the dashboard while tokens should be touched only by
    the API client, and separation makes it straightforward to grant narrower
    database privileges to read-only reporting roles later.

    These columns must never appear in an API response schema. There is no
    Pydantic model in this project that serialises them.
    """

    __tablename__ = "oauth_tokens"

    x_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("x_accounts.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    access_token_enc: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text, default=None)

    # Granted scopes, which may be narrower than requested. The collector checks
    # this before attempting anything, so a partially-approved consent screen
    # degrades gracefully instead of producing a wall of 403s.
    scopes: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)

    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    refresh_failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    x_account: Mapped[XAccount] = relationship(back_populates="oauth_token")

    def __repr__(self) -> str:
        # Never render token material, not even truncated.
        return f"<OAuthToken account={self.x_account_id} scopes={len(self.scopes)}>"

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


class AccountCapability(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Result of probing one X API capability for one account.

    Section 1.5 of the architecture doc: X's access rules change often and are
    documented inconsistently, so the system never hardcodes what it can do. It
    probes, records the answer here, and drives both the collector and the UI
    from this table. An endpoint that starts returning 403 next quarter turns a
    dashboard panel into "unavailable since <date>" instead of silently
    producing zeros.
    """

    __tablename__ = "account_capabilities"

    x_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("x_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    capability: Mapped[XCapability] = mapped_column(
        Enum(XCapability, name="x_capability"), nullable=False
    )
    status: Mapped[CapabilityStatus] = mapped_column(
        Enum(CapabilityStatus, name="capability_status"),
        default=CapabilityStatus.UNKNOWN,
        nullable=False,
    )

    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_available_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    details: Mapped[dict[str, object]] = mapped_column(JSONType, default=dict, nullable=False)

    x_account: Mapped[XAccount] = relationship(back_populates="capabilities")

    __table_args__ = (
        UniqueConstraint("x_account_id", "capability", name="uq_capability_per_account"),
        Index("ix_capabilities_status", "x_account_id", "status"),
    )

    def __repr__(self) -> str:
        return f"<Capability {self.capability.value}={self.status.value}>"
