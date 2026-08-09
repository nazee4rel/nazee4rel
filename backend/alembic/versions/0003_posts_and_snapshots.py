"""Phase 4: posts, metric snapshots and collection runs.

These are the irreplaceable tables. Once a post passes 30 days X stops returning
its non-public metrics, so `post_metric_snapshots` becomes the only record those
impressions will ever have.

Revision ID: 0003_posts_and_snapshots
Revises: 0002_oauth_state_and_usage
Create Date: 2026-08-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_posts_and_snapshots"
down_revision: str | None = "0002_oauth_state_and_usage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())

PROVENANCE = sa.Enum(
    "MEASURED",
    "DERIVED",
    "INFERRED",
    "USER_ENTERED",
    "IMPORTED",
    "UNAVAILABLE",
    name="provenance",
)


def upgrade() -> None:
    # ------------------------------------------------------------------ posts
    op.create_table(
        "posts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("x_account_id", sa.Uuid(), nullable=False),
        sa.Column("x_post_id", sa.String(length=32), nullable=False),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "post_type",
            sa.Enum("ORIGINAL", "REPLY", "QUOTE", "REPOST", name="post_type"),
            nullable=False,
            server_default="ORIGINAL",
        ),
        sa.Column("conversation_id", sa.String(length=32), nullable=True),
        sa.Column("in_reply_to_user_id", sa.String(length=32), nullable=True),
        sa.Column("lang", sa.String(length=8), nullable=True),
        # Deterministic format features, cheap to compute and highly predictive.
        sa.Column("has_media", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("has_link", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("has_poll", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_thread", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("thread_position", sa.Integer(), nullable=True),
        sa.Column("char_count", sa.Integer(), nullable=False, server_default="0"),
        # Kept so posts can be reprocessed without re-reading — which costs
        # money, and past 30 days is impossible at any price.
        sa.Column("raw_payload", JSONB, nullable=False, server_default="{}"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        # The cliff. The whole collection schedule exists to beat this.
        sa.Column("metrics_window_closes_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("final_freeze_at", sa.DateTime(timezone=True), nullable=True),
        # Distinguishes "never captured" from "captured as zero".
        sa.Column(
            "impressions_ever_collected", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "discovered_after_window_closed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["x_account_id"], ["x_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("x_account_id", "x_post_id", name="uq_posts_account_post"),
    )
    op.create_index("ix_posts_x_account_id", "posts", ["x_account_id"])
    op.create_index("ix_posts_account_posted", "posts", ["x_account_id", "posted_at"])
    op.create_index("ix_posts_metrics_window_closes_at", "posts", ["metrics_window_closes_at"])
    # Drives the scheduler's "which posts still need freezing" query.
    op.create_index(
        "ix_posts_freeze_pending", "posts", ["metrics_window_closes_at", "final_freeze_at"]
    )

    # ------------------------------------------------- post_metric_snapshots
    op.create_table(
        "post_metric_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("post_id", sa.Uuid(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("post_age_hours", sa.Float(), nullable=False),
        # Public metrics: available at any age.
        sa.Column("like_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reply_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retweet_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quote_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bookmark_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("public_impression_count", sa.Integer(), nullable=True),
        # Non-public: nullable on purpose. NULL means unavailable, which is a
        # different fact from 0 and must never render as a zero.
        sa.Column("impression_count", sa.Integer(), nullable=True),
        sa.Column("url_link_clicks", sa.Integer(), nullable=True),
        sa.Column("user_profile_clicks", sa.Integer(), nullable=True),
        sa.Column("organic_impression_count", sa.Integer(), nullable=True),
        sa.Column("organic_like_count", sa.Integer(), nullable=True),
        sa.Column("organic_reply_count", sa.Integer(), nullable=True),
        sa.Column("organic_retweet_count", sa.Integer(), nullable=True),
        sa.Column("provenance", PROVENANCE, nullable=False, server_default="MEASURED"),
        sa.Column("non_public_available", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_final_freeze", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # Makes retries idempotent rather than producing near-duplicate rows.
        sa.UniqueConstraint("post_id", "captured_at", name="uq_snapshot_post_time"),
    )
    op.create_index("ix_post_metric_snapshots_post_id", "post_metric_snapshots", ["post_id"])
    op.create_index("ix_snapshots_post_time", "post_metric_snapshots", ["post_id", "captured_at"])
    op.create_index("ix_snapshots_captured", "post_metric_snapshots", ["captured_at"])

    # ---------------------------------------------- account_metric_snapshots
    op.create_table(
        "account_metric_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("x_account_id", sa.Uuid(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("followers_count", sa.Integer(), nullable=False),
        sa.Column("following_count", sa.Integer(), nullable=True),
        sa.Column("post_count", sa.Integer(), nullable=True),
        sa.Column("listed_count", sa.Integer(), nullable=True),
        sa.Column("provenance", PROVENANCE, nullable=False, server_default="MEASURED"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["x_account_id"], ["x_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("x_account_id", "captured_at", name="uq_account_snapshot_time"),
    )
    op.create_index(
        "ix_account_metric_snapshots_x_account_id", "account_metric_snapshots", ["x_account_id"]
    )
    op.create_index(
        "ix_account_snapshots_time", "account_metric_snapshots", ["x_account_id", "captured_at"]
    )

    # -------------------------------------------------------- collection_runs
    op.create_table(
        "collection_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("x_account_id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "ACCOUNT_SNAPSHOT",
                "POST_DISCOVERY",
                "POST_METRICS",
                "FINAL_FREEZE",
                "BACKFILL",
                "CAPABILITY_PROBE",
                name="collection_kind",
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "RUNNING", "SUCCEEDED", "PARTIAL", "FAILED", "SKIPPED", name="collection_status"
            ),
            nullable=False,
            server_default="RUNNING",
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posts_discovered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("snapshots_written", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("resources_read", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_micros", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("degraded_reason", sa.String(length=64), nullable=True),
        # Non-zero on a FINAL_FREEZE run means impressions were permanently lost.
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("details", JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["x_account_id"], ["x_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_collection_runs_x_account_id", "collection_runs", ["x_account_id"])
    op.create_index(
        "ix_collection_runs_lookup", "collection_runs", ["x_account_id", "kind", "started_at"]
    )
    op.create_index("ix_collection_runs_status", "collection_runs", ["status", "started_at"])


def downgrade() -> None:
    op.drop_table("collection_runs")
    op.drop_table("account_metric_snapshots")
    op.drop_table("post_metric_snapshots")
    op.drop_table("posts")

    for enum_name in ("collection_status", "collection_kind", "provenance", "post_type"):
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
