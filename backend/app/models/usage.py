"""OAuth handshake state and the API spend ledger."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

JSONType = JSON().with_variant(JSONB(), "postgresql")


class OAuthState(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One pending OAuth 2.0 + PKCE handshake.

    The code verifier is the secret half of PKCE and is held server-side, keyed
    by the state parameter — never in a cookie or the URL, where an attacker who
    intercepted the authorization code could also obtain the verifier and defeat
    the point of the exchange.

    Rows are single-use and short-lived: `consumed_at` blocks replay of a
    callback, and `expires_at` bounds how long an unused credential sits in the
    database.
    """

    __tablename__ = "oauth_states"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    code_verifier: Mapped[str] = mapped_column(String(256), nullable=False)
    # Recorded so the callback can tell the user what they actually granted
    # versus what was asked for.
    requested_scopes: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    redirect_after: Mapped[str | None] = mapped_column(String(512), default=None)

    __table_args__ = (Index("ix_oauth_states_cleanup", "expires_at", "consumed_at"),)

    def is_usable(self, now: datetime) -> bool:
        expires = self.expires_at
        if expires.tzinfo is None:  # SQLite round-trips naive datetimes
            expires = expires.replace(tzinfo=now.tzinfo)
        return self.consumed_at is None and expires > now


class ApiUsageLedger(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One row per outbound X API call.

    Under pay-per-use, every read costs money, so spend has to be observable at
    request granularity rather than discovered on a monthly invoice. This table
    is what the cost governor reads to decide whether the next call is
    affordable, and what the dashboard shows the user.

    Costs are integer micro-USD (1e-6 USD). Money is never a float here: these
    rows are summed in their tens of thousands and rounding drift in a spend
    ledger is not acceptable.
    """

    __tablename__ = "api_usage_ledger"

    x_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("x_accounts.id", ondelete="CASCADE"), default=None, index=True
    )

    endpoint_key: Mapped[str] = mapped_column(String(64), nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    cost_class: Mapped[str] = mapped_column(String(16), nullable=False)

    # What we actually got back. Billing is per resource, not per request, so
    # this is the quantity that matters — not the number of calls.
    resources_returned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_cost_micros: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    billing_mode: Mapped[str] = mapped_column(String(24), nullable=False)

    status_code: Mapped[int | None] = mapped_column(Integer, default=None)
    latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    rate_limit_remaining: Mapped[int | None] = mapped_column(Integer, default=None)
    rate_limit_reset_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    # True when the request never left the process — blocked by the budget
    # governor, a closed rate-limit bucket, or an unavailable capability. These
    # rows cost nothing but explain gaps in collection.
    was_blocked: Mapped[bool] = mapped_column(default=False, nullable=False)
    block_reason: Mapped[str | None] = mapped_column(String(64), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    context: Mapped[dict[str, object]] = mapped_column(JSONType, default=dict, nullable=False)

    __table_args__ = (
        # Budget windows are always "this calendar month for this account".
        Index("ix_usage_account_time", "x_account_id", "created_at"),
        Index("ix_usage_endpoint_time", "endpoint_key", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<ApiUsage {self.endpoint_key} resources={self.resources_returned} "
            f"cost={self.estimated_cost_micros}µ$>"
        )
