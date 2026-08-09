"""Append-only audit trail.

Rows are never updated or deleted. This is the record that answers "who
approved the agent posting that?" — which matters from Phase 6 onward, when the
agent can draft posts for approval.
"""

from __future__ import annotations

import uuid

from sqlalchemy import JSON, Enum, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import AuditAction

JSONType = JSON().with_variant(JSONB(), "postgresql")


class AuditLog(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "audit_logs"

    # Nullable because failed logins have no authenticated user yet, and we
    # very much want those recorded.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None, index=True
    )
    x_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("x_accounts.id", ondelete="SET NULL"), default=None, index=True
    )

    action: Mapped[AuditAction] = mapped_column(
        Enum(AuditAction, name="audit_action"), nullable=False
    )
    target_type: Mapped[str | None] = mapped_column(String(64), default=None)
    target_id: Mapped[str | None] = mapped_column(String(64), default=None)

    ip_address: Mapped[str | None] = mapped_column(String(45), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(512), default=None)

    # Structured context. Must never contain secrets — the logging scrubber
    # covers log output, but this is a database write, so callers are
    # responsible. `AuditService` enforces it.
    context: Mapped[dict[str, object]] = mapped_column(JSONType, default=dict, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, default=None)

    __table_args__ = (
        Index("ix_audit_user_time", "user_id", "created_at"),
        Index("ix_audit_action_time", "action", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<AuditLog {self.action.value} user={self.user_id}>"


class SystemLog(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Operational errors and warnings surfaced on the Agent Activity page.

    Distinct from `audit_logs`: that records human intent, this records machine
    trouble. Users need to see collection failures, because a failed collection
    inside the 30-day window is permanent data loss.
    """

    __tablename__ = "system_logs"

    x_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("x_accounts.id", ondelete="CASCADE"), default=None, index=True
    )
    level: Mapped[str] = mapped_column(String(16), nullable=False)  # INFO | WARNING | ERROR
    component: Mapped[str] = mapped_column(String(64), nullable=False)  # collector, agent, api
    message: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict[str, object]] = mapped_column(JSONType, default=dict, nullable=False)

    __table_args__ = (Index("ix_system_logs_lookup", "x_account_id", "level", "created_at"),)
