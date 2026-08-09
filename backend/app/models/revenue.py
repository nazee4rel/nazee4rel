"""Revenue, campaigns and sponsorships.

**None of this comes from X.** There is no developer API for creator earnings —
Ads Revenue Share, Subscriptions and Tips all lack endpoints. Every row here is
therefore `USER_ENTERED` or `IMPORTED`, and the schema records which so the
dashboard can never present it with the same authority as measured data.

Money is stored as integer minor units (cents, kobo, pence). Never floats:
these values are summed, apportioned across posts, and divided into impressions
to produce RPM, and binary floating point would accumulate error through all of
it.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import Provenance

if TYPE_CHECKING:
    from app.models.content import Post

JSONType = JSON().with_variant(JSONB(), "postgresql")


class RevenueSourceType(enum.StrEnum):
    X_ADS_SHARE = "X_ADS_SHARE"
    X_SUBSCRIPTIONS = "X_SUBSCRIPTIONS"
    X_TIPS = "X_TIPS"
    SPONSORSHIP = "SPONSORSHIP"
    AFFILIATE = "AFFILIATE"
    BRAND_DEAL = "BRAND_DEAL"
    OTHER = "OTHER"


class CampaignStatus(enum.StrEnum):
    PLANNED = "PLANNED"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class Campaign(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "campaigns"

    x_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("x_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    sponsor: Mapped[str | None] = mapped_column(String(200), default=None)
    status: Mapped[CampaignStatus] = mapped_column(
        Enum(CampaignStatus, name="campaign_status"),
        default=CampaignStatus.PLANNED,
        nullable=False,
    )

    start_date: Mapped[date | None] = mapped_column(Date, default=None)
    end_date: Mapped[date | None] = mapped_column(Date, default=None)

    contracted_amount_minor: Mapped[int | None] = mapped_column(BigInteger, default=None)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, default=None)

    entries: Mapped[list[RevenueEntry]] = relationship(back_populates="campaign")

    __table_args__ = (Index("ix_campaigns_account_status", "x_account_id", "status"),)


class Sponsorship(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Deliverables agreed for a campaign.

    Separate from `campaigns` because one campaign often carries several
    obligations with different due dates and rates.
    """

    __tablename__ = "sponsorships"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    deliverable: Mapped[str] = mapped_column(String(300), nullable=False)
    agreed_amount_minor: Mapped[int | None] = mapped_column(BigInteger, default=None)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date, default=None)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    notes: Mapped[str | None] = mapped_column(Text, default=None)


class RevenueEntry(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "revenue_entries"

    x_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("x_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_type: Mapped[RevenueSourceType] = mapped_column(
        Enum(RevenueSourceType, name="revenue_source_type"), nullable=False
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL"), default=None, index=True
    )
    # Nullable, and usually null: most revenue cannot honestly be pinned to one
    # post. Forcing an attribution would manufacture precision.
    post_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("posts.id", ondelete="SET NULL"), default=None, index=True
    )

    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    earned_at: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    provenance: Mapped[Provenance] = mapped_column(
        Enum(Provenance, name="provenance"), default=Provenance.USER_ENTERED, nullable=False
    )
    # External reference (a payout id, a CSV row hash) used to make re-imports
    # idempotent rather than doubling every figure.
    external_ref: Mapped[str | None] = mapped_column(String(200), default=None)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    import_batch: Mapped[str | None] = mapped_column(String(64), default=None)
    details: Mapped[dict[str, object]] = mapped_column(JSONType, default=dict, nullable=False)

    campaign: Mapped[Campaign | None] = relationship(back_populates="entries")
    post: Mapped[Post | None] = relationship()

    __table_args__ = (
        # Makes re-importing the same statement a no-op.
        UniqueConstraint("x_account_id", "external_ref", name="uq_revenue_external_ref"),
        Index("ix_revenue_account_date", "x_account_id", "earned_at"),
        Index("ix_revenue_source", "x_account_id", "source_type", "earned_at"),
    )

    @property
    def amount(self) -> float:
        """Major units, for display only. Never use for arithmetic."""
        return self.amount_minor / 100

    def __repr__(self) -> str:
        return f"<Revenue {self.source_type.value} {self.amount_minor}minor {self.earned_at}>"


class RevenueAttribution(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Explicit split of one revenue entry across several posts.

    A separate table rather than a hidden heuristic, so the basis of any
    per-post revenue figure is inspectable and can be corrected.
    """

    __tablename__ = "revenue_attributions"

    revenue_entry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("revenue_entries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    post_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # How the split was decided: "manual", "equal_split", "impression_weighted".
    method: Mapped[str] = mapped_column(String(40), default="manual", nullable=False)
    share: Mapped[float | None] = mapped_column(Numeric(6, 5), default=None)

    __table_args__ = (
        UniqueConstraint("revenue_entry_id", "post_id", name="uq_attribution_entry_post"),
    )


class Topic(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A controlled, user-editable taxonomy label.

    Deliberately not free-form. Free-form labels drift between runs, which makes
    cross-period comparison meaningless — the one thing topic analysis is for.
    Assignment happens in Phase 6; the schema lands here so the analytics that
    consume it can be built and tested first.
    """

    __tablename__ = "topics"

    x_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("x_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    __table_args__ = (UniqueConstraint("x_account_id", "name", name="uq_topic_account_name"),)


class PostTopic(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Assignment of a topic to a post, with its provenance.

    `classified_by` records the model version, so a taxonomy or model change is
    traceable instead of silently rewriting history.
    """

    __tablename__ = "post_topics"

    post_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    topic_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"), nullable=False, index=True
    )
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), default=None)
    classified_by: Mapped[str] = mapped_column(String(80), default="unassigned", nullable=False)
    provenance: Mapped[Provenance] = mapped_column(
        Enum(Provenance, name="provenance"), default=Provenance.INFERRED, nullable=False
    )

    __table_args__ = (UniqueConstraint("post_id", "topic_id", name="uq_post_topic"),)
