"""Declarative base and shared column mixins."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.security import uuid7


class Base(DeclarativeBase):
    """Base for all ORM models.

    Alembic autogenerate reads `Base.metadata`, so every model module must be
    imported in `app.models.__init__` or its table will silently be missing
    from generated migrations.
    """


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SoftDeleteMixin:
    """Soft deletion for user-owned data.

    Analytics history is expensive-to-impossible to reacquire (impressions
    older than 30 days cannot be re-fetched from X at any price), so nothing
    user-facing is ever hard-deleted.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None
