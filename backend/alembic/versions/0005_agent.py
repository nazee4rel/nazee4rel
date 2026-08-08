"""Phase 6: agent runs, insights, recommendations and gated actions.

`agent_runs.evidence` holds the exact fact bundle the model was shown, so an
insight remains auditable long after the run. `recommendations` stores a
prediction — metric, direction, measured baseline, verification date — because
advice that cannot be shown wrong cannot be learned from. `agent_actions`
records proposals that were blocked or rejected as well as those that ran; the
refusals are the more informative half of that trail.

Revision ID: 0005_agent
Revises: 0004_revenue_and_topics
Create Date: 2026-08-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005_agent"
down_revision: str | None = "0004_revenue_and_topics"
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
    # ------------------------------------------------------------- agent_runs
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("x_account_id", sa.Uuid(), nullable=False),
        sa.Column(
            "trigger",
            sa.Enum("SCHEDULED", "MANUAL", "ALERT", name="agent_run_trigger"),
            nullable=False,
            server_default="SCHEDULED",
        ),
        sa.Column(
            "status",
            sa.Enum(
                "RUNNING", "COMPLETED", "PARTIAL", "SKIPPED", "FAILED", name="agent_run_status"
            ),
            nullable=False,
            server_default="RUNNING",
        ),
        sa.Column(
            "stage",
            sa.Enum(
                "OBSERVE",
                "COLLECT",
                "ANALYZE",
                "REASON",
                "RECOMMEND",
                "ACT",
                "VERIFY",
                "REPORT",
                name="agent_stage",
            ),
            nullable=False,
            server_default="OBSERVE",
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stage_log", JSONB, nullable=False, server_default="{}"),
        # The audit anchor: what the model actually saw.
        sa.Column("evidence", JSONB, nullable=False, server_default="{}"),
        sa.Column("model", sa.String(length=80), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "ingested_untrusted_content", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("insights_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("recommendations_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("actions_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("grounding_rejections", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["x_account_id"], ["x_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_runs_x_account_id", "agent_runs", ["x_account_id"])
    op.create_index("ix_agent_runs_account_started", "agent_runs", ["x_account_id", "started_at"])

    # --------------------------------------------------------------- insights
    op.create_table(
        "insights",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("x_account_id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "GROWTH",
                "ENGAGEMENT",
                "CONTENT",
                "TIMING",
                "REVENUE",
                "RISK",
                "DATA_QUALITY",
                name="insight_kind",
            ),
            nullable=False,
        ),
        sa.Column(
            "severity",
            sa.Enum("INFO", "NOTABLE", "URGENT", name="insight_severity"),
            nullable=False,
            server_default="INFO",
        ),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        # Verified against the run's evidence before the row is written.
        sa.Column("supporting_data", JSONB, nullable=False, server_default="{}"),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("provenance", PROVENANCE, nullable=False, server_default="INFERRED"),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["x_account_id"], ["x_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_insights_agent_run_id", "insights", ["agent_run_id"])
    op.create_index("ix_insights_x_account_id", "insights", ["x_account_id"])
    op.create_index("ix_insights_account_created", "insights", ["x_account_id", "created_at"])
    op.create_index("ix_insights_account_kind", "insights", ["x_account_id", "kind"])

    # -------------------------------------------------------- recommendations
    op.create_table(
        "recommendations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("x_account_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("supporting_data", JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "predicted_metric",
            sa.Enum(
                "FOLLOWER_GROWTH_DAILY",
                "ENGAGEMENT_RATE_MEDIAN",
                "POSTS_PER_WEEK",
                "IMPRESSIONS_MEDIAN",
                name="predicted_metric",
            ),
            nullable=False,
        ),
        sa.Column(
            "predicted_direction",
            sa.Enum("INCREASE", "DECREASE", "MAINTAIN", name="predicted_direction"),
            nullable=False,
        ),
        # Measured by the system at proposal time, never supplied by the model.
        sa.Column("baseline_value", sa.Float(), nullable=True),
        sa.Column("baseline_note", sa.Text(), nullable=True),
        sa.Column("verify_after", sa.Date(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PROPOSED", "ACCEPTED", "DISMISSED", "SUPERSEDED", name="recommendation_status"
            ),
            nullable=False,
            server_default="PROPOSED",
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "verification_result",
            sa.Enum("PENDING", "CONFIRMED", "REFUTED", "INCONCLUSIVE", name="verification_result"),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_value", sa.Float(), nullable=True),
        sa.Column("verification_note", sa.Text(), nullable=True),
        sa.Column("provenance", PROVENANCE, nullable=False, server_default="INFERRED"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["x_account_id"], ["x_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_recommendations_agent_run_id", "recommendations", ["agent_run_id"])
    op.create_index("ix_recommendations_x_account_id", "recommendations", ["x_account_id"])
    op.create_index("ix_recommendations_verify_after", "recommendations", ["verify_after"])
    op.create_index(
        "ix_recommendations_account_status", "recommendations", ["x_account_id", "status"]
    )
    op.create_index(
        "ix_recommendations_due", "recommendations", ["verification_result", "verify_after"]
    )

    # ---------------------------------------------------------- agent_actions
    op.create_table(
        "agent_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=True),
        sa.Column("x_account_id", sa.Uuid(), nullable=False),
        sa.Column("recommendation_id", sa.Uuid(), nullable=True),
        sa.Column(
            "action_type",
            sa.Enum(
                "RECORD_NOTE",
                "RAISE_ALERT",
                "DRAFT_POST",
                "PROPOSE_TOPIC",
                "PUBLISH_POST",
                name="agent_action_type",
            ),
            nullable=False,
        ),
        sa.Column(
            "autonomy_tier",
            sa.Enum("T0", "T1", "T2", "T3", name="autonomy_tier"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING_APPROVAL",
                "APPROVED",
                "REJECTED",
                "EXECUTED",
                "FAILED",
                "EXPIRED",
                "BLOCKED",
                name="agent_action_status",
            ),
            nullable=False,
            server_default="PENDING_APPROVAL",
        ),
        sa.Column("payload", JSONB, nullable=False, server_default="{}"),
        sa.Column("summary", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("requires_approval", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("approved_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", JSONB, nullable=False, server_default="{}"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("blocked_reason", sa.Text(), nullable=True),
        sa.Column(
            "ingested_untrusted_content", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["x_account_id"], ["x_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recommendation_id"], ["recommendations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["approved_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_actions_agent_run_id", "agent_actions", ["agent_run_id"])
    op.create_index("ix_agent_actions_x_account_id", "agent_actions", ["x_account_id"])
    op.create_index("ix_agent_actions_recommendation_id", "agent_actions", ["recommendation_id"])
    op.create_index("ix_agent_actions_expires_at", "agent_actions", ["expires_at"])
    op.create_index("ix_agent_actions_account_status", "agent_actions", ["x_account_id", "status"])
    op.create_index("ix_agent_actions_pending", "agent_actions", ["status", "expires_at"])


def downgrade() -> None:
    op.drop_table("agent_actions")
    op.drop_table("recommendations")
    op.drop_table("insights")
    op.drop_table("agent_runs")

    # `provenance` is owned by migration 0003, so it is deliberately not dropped.
    for enum_name in (
        "agent_action_status",
        "autonomy_tier",
        "agent_action_type",
        "verification_result",
        "recommendation_status",
        "predicted_direction",
        "predicted_metric",
        "insight_severity",
        "insight_kind",
        "agent_stage",
        "agent_run_status",
        "agent_run_trigger",
    ):
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
