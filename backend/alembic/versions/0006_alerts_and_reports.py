"""Phase 8: alert rules, alerts, reports and their delivery record.

`alerts.dedupe_key` is uniquely constrained per account, which is what makes the
same condition observed on eight consecutive runs one row rather than eight.
Deliveries are a separate table so a failed email is a recorded failure against
an alert that still exists, rather than an alert that never happened.

Revision ID: 0006_alerts_and_reports
Revises: 0005_agent
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006_alerts_and_reports"
down_revision: str | None = "0005_agent"
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

RULE_KEYS = (
    "FOLLOWER_SPIKE",
    "FOLLOWER_DROP",
    "BREAKOUT_POST",
    "ENGAGEMENT_DECLINE",
    "REVENUE_CHANGE",
    "UNUSUAL_CHURN",
    "COLLECTION_STALLED",
    "FREEZE_AT_RISK",
    "BUDGET_PRESSURE",
    "ACCESS_DEGRADED",
)


def upgrade() -> None:
    # ------------------------------------------------------------ alert_rules
    op.create_table(
        "alert_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("x_account_id", sa.Uuid(), nullable=False),
        sa.Column("rule_key", sa.Enum(*RULE_KEYS, name="alert_rule_key"), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        # The single most effective control against alert fatigue.
        sa.Column("cooldown_hours", sa.Integer(), nullable=False, server_default="24"),
        sa.Column(
            "min_severity",
            sa.Enum("INFO", "WARNING", "CRITICAL", name="alert_severity"),
            nullable=False,
            server_default="INFO",
        ),
        sa.Column("thresholds", JSONB, nullable=False, server_default="{}"),
        sa.Column("channels", JSONB, nullable=False, server_default="[]"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["x_account_id"], ["x_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("x_account_id", "rule_key", name="uq_alert_rule_account_key"),
    )
    op.create_index("ix_alert_rules_x_account_id", "alert_rules", ["x_account_id"])

    # ----------------------------------------------------------------- alerts
    op.create_table(
        "alerts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("x_account_id", sa.Uuid(), nullable=False),
        sa.Column("rule_key", sa.Enum(*RULE_KEYS, name="alert_rule_key"), nullable=False),
        sa.Column(
            "severity",
            sa.Enum("INFO", "WARNING", "CRITICAL", name="alert_severity"),
            nullable=False,
        ),
        sa.Column(
            "state",
            sa.Enum("FIRING", "ACKNOWLEDGED", "RESOLVED", name="alert_state"),
            nullable=False,
            server_default="FIRING",
        ),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("facts", JSONB, nullable=False, server_default="{}"),
        sa.Column("provenance", PROVENANCE, nullable=False, server_default="DERIVED"),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["x_account_id"], ["x_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["acknowledged_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        # The deduplication guarantee, enforced by the database rather than by
        # the service remembering to check.
        sa.UniqueConstraint("x_account_id", "dedupe_key", name="uq_alert_dedupe"),
    )
    op.create_index("ix_alerts_x_account_id", "alerts", ["x_account_id"])
    op.create_index("ix_alerts_account_state", "alerts", ["x_account_id", "state"])
    op.create_index("ix_alerts_account_fired", "alerts", ["x_account_id", "fired_at"])

    # ---------------------------------------------------------------- reports
    op.create_table(
        "reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("x_account_id", sa.Uuid(), nullable=False),
        sa.Column(
            "period",
            sa.Enum("DAILY", "WEEKLY", "MONTHLY", name="report_period"),
            nullable=False,
        ),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        # Structured rather than rendered, so one generation serves both the
        # dashboard and the email.
        sa.Column("sections", JSONB, nullable=False, server_default="[]"),
        sa.Column(
            "status",
            sa.Enum("GENERATED", "DELIVERED", "DELIVERY_FAILED", name="report_status"),
            nullable=False,
            server_default="GENERATED",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["x_account_id"], ["x_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("x_account_id", "period", "period_start", name="uq_report_period"),
    )
    op.create_index("ix_reports_x_account_id", "reports", ["x_account_id"])
    op.create_index("ix_reports_account_generated", "reports", ["x_account_id", "generated_at"])

    # ------------------------------------------------------------- deliveries
    op.create_table(
        "deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("alert_id", sa.Uuid(), nullable=True),
        sa.Column("report_id", sa.Uuid(), nullable=True),
        sa.Column(
            "channel",
            sa.Enum("DASHBOARD", "EMAIL", name="delivery_channel"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("PENDING", "SENT", "FAILED", "SKIPPED", name="delivery_status"),
            nullable=False,
            server_default="PENDING",
        ),
        # Redacted at write time; never a full address book.
        sa.Column("target", sa.String(length=200), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["alert_id"], ["alerts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_deliveries_alert_id", "deliveries", ["alert_id"])
    op.create_index("ix_deliveries_report_id", "deliveries", ["report_id"])
    op.create_index("ix_deliveries_channel_status", "deliveries", ["channel", "status"])


def downgrade() -> None:
    op.drop_table("deliveries")
    op.drop_table("reports")
    op.drop_table("alerts")
    op.drop_table("alert_rules")

    # `provenance` is owned by migration 0003, so it is deliberately not dropped.
    for enum_name in (
        "delivery_status",
        "delivery_channel",
        "report_status",
        "report_period",
        "alert_state",
        "alert_severity",
        "alert_rule_key",
    ):
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
