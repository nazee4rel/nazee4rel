"""Phase 5: revenue, campaigns, sponsorships and the topic taxonomy.

None of the revenue data originates from X — there is no creator-earnings API —
so every row is USER_ENTERED or IMPORTED, and the provenance column records
which.

Revision ID: 0004_revenue_and_topics
Revises: 0003_posts_and_snapshots
Create Date: 2026-08-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004_revenue_and_topics"
down_revision: str | None = "0003_posts_and_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())

# Created by migration 0003; referenced without re-creating it.
PROVENANCE = postgresql.ENUM(
    "MEASURED",
    "DERIVED",
    "INFERRED",
    "USER_ENTERED",
    "IMPORTED",
    "UNAVAILABLE",
    name="provenance",
    create_type=False,
)


def upgrade() -> None:
    # -------------------------------------------------------------- campaigns
    op.create_table(
        "campaigns",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("x_account_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("sponsor", sa.String(length=200), nullable=True),
        sa.Column(
            "status",
            sa.Enum("PLANNED", "ACTIVE", "COMPLETED", "CANCELLED", name="campaign_status"),
            nullable=False,
            server_default="PLANNED",
        ),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        # Integer minor units throughout. Money is never a float here.
        sa.Column("contracted_amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["x_account_id"], ["x_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_campaigns_x_account_id", "campaigns", ["x_account_id"])
    op.create_index("ix_campaigns_account_status", "campaigns", ["x_account_id", "status"])

    # ----------------------------------------------------------- sponsorships
    op.create_table(
        "sponsorships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("deliverable", sa.String(length=300), nullable=False),
        sa.Column("agreed_amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sponsorships_campaign_id", "sponsorships", ["campaign_id"])

    # -------------------------------------------------------- revenue_entries
    op.create_table(
        "revenue_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("x_account_id", sa.Uuid(), nullable=False),
        sa.Column(
            "source_type",
            sa.Enum(
                "X_ADS_SHARE",
                "X_SUBSCRIPTIONS",
                "X_TIPS",
                "SPONSORSHIP",
                "AFFILIATE",
                "BRAND_DEAL",
                "OTHER",
                name="revenue_source_type",
            ),
            nullable=False,
        ),
        sa.Column("campaign_id", sa.Uuid(), nullable=True),
        # Usually null: most revenue cannot honestly be pinned to one post.
        sa.Column("post_id", sa.Uuid(), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("earned_at", sa.Date(), nullable=False),
        sa.Column("provenance", PROVENANCE, nullable=False, server_default="USER_ENTERED"),
        sa.Column("external_ref", sa.String(length=200), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("import_batch", sa.String(length=64), nullable=True),
        sa.Column("details", JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["x_account_id"], ["x_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        # Re-importing the same statement must be a no-op, not a doubling.
        sa.UniqueConstraint("x_account_id", "external_ref", name="uq_revenue_external_ref"),
    )
    op.create_index("ix_revenue_entries_x_account_id", "revenue_entries", ["x_account_id"])
    op.create_index("ix_revenue_entries_campaign_id", "revenue_entries", ["campaign_id"])
    op.create_index("ix_revenue_entries_post_id", "revenue_entries", ["post_id"])
    op.create_index("ix_revenue_entries_earned_at", "revenue_entries", ["earned_at"])
    op.create_index("ix_revenue_account_date", "revenue_entries", ["x_account_id", "earned_at"])
    op.create_index(
        "ix_revenue_source", "revenue_entries", ["x_account_id", "source_type", "earned_at"]
    )

    # --------------------------------------------------- revenue_attributions
    op.create_table(
        "revenue_attributions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("revenue_entry_id", sa.Uuid(), nullable=False),
        sa.Column("post_id", sa.Uuid(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        # Recorded so a per-post revenue figure can be traced to how it was split.
        sa.Column("method", sa.String(length=40), nullable=False, server_default="manual"),
        sa.Column("share", sa.Numeric(precision=6, scale=5), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["revenue_entry_id"], ["revenue_entries.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("revenue_entry_id", "post_id", name="uq_attribution_entry_post"),
    )
    op.create_index(
        "ix_revenue_attributions_revenue_entry_id", "revenue_attributions", ["revenue_entry_id"]
    )
    op.create_index("ix_revenue_attributions_post_id", "revenue_attributions", ["post_id"])

    # ----------------------------------------------------------------- topics
    op.create_table(
        "topics",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("x_account_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["x_account_id"], ["x_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("x_account_id", "name", name="uq_topic_account_name"),
    )
    op.create_index("ix_topics_x_account_id", "topics", ["x_account_id"])

    op.create_table(
        "post_topics",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("post_id", sa.Uuid(), nullable=False),
        sa.Column("topic_id", sa.Uuid(), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        # Model version, so a reclassification is traceable rather than silent.
        sa.Column(
            "classified_by", sa.String(length=80), nullable=False, server_default="unassigned"
        ),
        sa.Column("provenance", PROVENANCE, nullable=False, server_default="INFERRED"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("post_id", "topic_id", name="uq_post_topic"),
    )
    op.create_index("ix_post_topics_post_id", "post_topics", ["post_id"])
    op.create_index("ix_post_topics_topic_id", "post_topics", ["topic_id"])


def downgrade() -> None:
    op.drop_table("post_topics")
    op.drop_table("topics")
    op.drop_table("revenue_attributions")
    op.drop_table("revenue_entries")
    op.drop_table("sponsorships")
    op.drop_table("campaigns")

    # `provenance` is owned by migration 0003, so it is deliberately not dropped.
    for enum_name in ("revenue_source_type", "campaign_status"):
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
