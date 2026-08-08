"""Durable record of every collection cycle.

A collection failure inside the 30-day window is permanent data loss, so runs
are recorded rather than merely logged: the Agent Activity page needs to show
what ran, what it cost, what it skipped and why.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

JSONType = JSON().with_variant(JSONB(), "postgresql")


class CollectionKind(enum.StrEnum):
    ACCOUNT_SNAPSHOT = "ACCOUNT_SNAPSHOT"  # hourly follower count
    POST_DISCOVERY = "POST_DISCOVERY"  # find new posts
    POST_METRICS = "POST_METRICS"  # snapshot metrics on the decay ladder
    FINAL_FREEZE = "FINAL_FREEZE"  # mandatory pre-cliff capture
    BACKFILL = "BACKFILL"  # one-off at connect time
    CAPABILITY_PROBE = "CAPABILITY_PROBE"


class CollectionStatus(enum.StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"  # some work done, some skipped or failed
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"  # deliberately not run (budget, capability, nothing due)


class CollectionRun(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "collection_runs"

    x_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("x_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[CollectionKind] = mapped_column(
        Enum(CollectionKind, name="collection_kind"), nullable=False
    )
    status: Mapped[CollectionStatus] = mapped_column(
        Enum(CollectionStatus, name="collection_status"),
        default=CollectionStatus.RUNNING,
        nullable=False,
    )

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    posts_discovered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    snapshots_written: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    resources_read: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_micros: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Set when the cost governor forced a reduced cadence. Surfaced in the UI,
    # because a quietly degraded collector produces quietly incomplete data.
    degraded_reason: Mapped[str | None] = mapped_column(String(64), default=None)
    # Posts that were due but skipped. Non-zero here on a FINAL_FREEZE run means
    # impressions were permanently lost, which warrants an alert in Phase 8.
    skipped_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    error: Mapped[str | None] = mapped_column(Text, default=None)
    details: Mapped[dict[str, object]] = mapped_column(JSONType, default=dict, nullable=False)

    __table_args__ = (
        Index("ix_collection_runs_lookup", "x_account_id", "kind", "started_at"),
        Index("ix_collection_runs_status", "status", "started_at"),
    )

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    def __repr__(self) -> str:
        return f"<CollectionRun {self.kind.value} {self.status.value}>"
