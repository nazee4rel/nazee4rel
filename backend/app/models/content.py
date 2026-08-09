"""Posts and the append-only metric snapshot tables.

These are the irreplaceable part of the system. X returns `non_public_metrics`
only for posts under 30 days old, so once that window closes these rows are the
*only* place those impressions will ever exist. Nothing here is ever updated in
place and nothing is ever hard-deleted.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
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
from app.models.enums import Provenance

if TYPE_CHECKING:
    from app.models.x_account import XAccount

JSONType = JSON().with_variant(JSONB(), "postgresql")


class PostType(enum.StrEnum):
    ORIGINAL = "ORIGINAL"
    REPLY = "REPLY"
    QUOTE = "QUOTE"
    REPOST = "REPOST"


class Post(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "posts"

    x_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("x_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    x_post_id: Mapped[str] = mapped_column(String(32), nullable=False)

    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    post_type: Mapped[PostType] = mapped_column(
        Enum(PostType, name="post_type"), default=PostType.ORIGINAL, nullable=False
    )

    conversation_id: Mapped[str | None] = mapped_column(String(32), default=None)
    in_reply_to_user_id: Mapped[str | None] = mapped_column(String(32), default=None)
    lang: Mapped[str | None] = mapped_column(String(8), default=None)

    # Deterministic format features. Cheap to compute, highly predictive, and
    # the basis for "which formats work" in Phase 5.
    has_media: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    has_link: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    has_poll: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_thread: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    thread_position: Mapped[int | None] = mapped_column(Integer, default=None)
    char_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Kept so the post can be reprocessed without re-fetching. That matters
    # more than usual here: re-reading costs money, and past 30 days the
    # private metrics cannot be re-read at any price.
    raw_payload: Mapped[dict[str, object]] = mapped_column(JSONType, default=dict, nullable=False)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # --- 30-day cliff bookkeeping ------------------------------------------
    # When X stops returning non_public_metrics for this post. Everything in
    # the collection schedule is organised around beating this timestamp.
    metrics_window_closes_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    # Set when the mandatory pre-cliff snapshot succeeds.
    final_freeze_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # Distinguishes "we never captured impressions for this post" from "this
    # post had zero impressions". Without it the dashboard cannot tell a gap
    # from a genuine zero, which is the exact failure this project avoids.
    impressions_ever_collected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # True when the post was already past the window when first discovered —
    # its impressions are permanently unavailable, not merely missing.
    discovered_after_window_closed: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    x_account: Mapped[XAccount] = relationship()
    snapshots: Mapped[list[PostMetricSnapshot]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("x_account_id", "x_post_id", name="uq_posts_account_post"),
        Index("ix_posts_account_posted", "x_account_id", "posted_at"),
        # Drives the collection scheduler's "what is due" query.
        Index("ix_posts_freeze_pending", "metrics_window_closes_at", "final_freeze_at"),
    )

    def __repr__(self) -> str:
        return f"<Post {self.x_post_id} posted={self.posted_at:%Y-%m-%d}>"


class PostMetricSnapshot(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One observation of one post's metrics at one moment.

    Append-only. The series of these rows is what makes engagement velocity,
    breakout detection and follower attribution possible — a single current
    value could not support any of them.
    """

    __tablename__ = "post_metric_snapshots"

    post_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    post_age_hours: Mapped[float] = mapped_column(Float, nullable=False)

    # --- public metrics: available for any post, at any age ----------------
    like_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reply_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retweet_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    quote_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bookmark_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    public_impression_count: Mapped[int | None] = mapped_column(Integer, default=None)

    # --- non-public metrics: own posts, under 30 days, user context only ---
    # Nullable on purpose. NULL means "not available", which is categorically
    # different from 0 and must never be rendered as a zero.
    impression_count: Mapped[int | None] = mapped_column(Integer, default=None)
    url_link_clicks: Mapped[int | None] = mapped_column(Integer, default=None)
    user_profile_clicks: Mapped[int | None] = mapped_column(Integer, default=None)

    # --- organic breakdown --------------------------------------------------
    organic_impression_count: Mapped[int | None] = mapped_column(Integer, default=None)
    organic_like_count: Mapped[int | None] = mapped_column(Integer, default=None)
    organic_reply_count: Mapped[int | None] = mapped_column(Integer, default=None)
    organic_retweet_count: Mapped[int | None] = mapped_column(Integer, default=None)

    provenance: Mapped[Provenance] = mapped_column(
        Enum(Provenance, name="provenance"), default=Provenance.MEASURED, nullable=False
    )
    # Records whether X actually returned the private fields on this call, so a
    # later reader can tell a genuine absence from a collector bug.
    non_public_available: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # The mandatory pre-cliff capture. After this, impressions are frozen here
    # forever because X will not return them again.
    is_final_freeze: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    post: Mapped[Post] = relationship(back_populates="snapshots")

    __table_args__ = (
        # One observation per post per instant; makes retries idempotent.
        UniqueConstraint("post_id", "captured_at", name="uq_snapshot_post_time"),
        Index("ix_snapshots_post_time", "post_id", "captured_at"),
        Index("ix_snapshots_captured", "captured_at"),
    )

    @property
    def total_engagement(self) -> int:
        return (
            self.like_count
            + self.reply_count
            + self.retweet_count
            + self.quote_count
            + self.bookmark_count
        )

    def __repr__(self) -> str:
        return f"<Snapshot post={self.post_id} at={self.captured_at:%Y-%m-%d %H:%M}>"


class AccountMetricSnapshot(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Hourly follower-count observation.

    X exposes only a current follower count and no history endpoint, so this
    table *is* the history. Hourly resolution is not extravagance — it is the
    minimum that makes the Phase 5 follower-attribution model able to separate
    one post's effect from another's.
    """

    __tablename__ = "account_metric_snapshots"

    x_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("x_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    followers_count: Mapped[int] = mapped_column(Integer, nullable=False)
    following_count: Mapped[int | None] = mapped_column(Integer, default=None)
    post_count: Mapped[int | None] = mapped_column(Integer, default=None)
    listed_count: Mapped[int | None] = mapped_column(Integer, default=None)

    provenance: Mapped[Provenance] = mapped_column(
        Enum(Provenance, name="provenance"), default=Provenance.MEASURED, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("x_account_id", "captured_at", name="uq_account_snapshot_time"),
        Index("ix_account_snapshots_time", "x_account_id", "captured_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<AccountSnapshot followers={self.followers_count} at={self.captured_at:%Y-%m-%d %H}>"
        )
