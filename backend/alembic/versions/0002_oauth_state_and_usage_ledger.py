"""Phase 3: OAuth handshake state and the API spend ledger.

Revision ID: 0002_oauth_state_and_usage
Revises: 0001_identity_and_access
Create Date: 2026-08-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002_oauth_state_and_usage"
down_revision: str | None = "0001_identity_and_access"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    # ----------------------------------------------------------- oauth_states
    op.create_table(
        "oauth_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=128), nullable=False),
        # PKCE secret half. Server-side only — never a cookie, never in a URL.
        sa.Column("code_verifier", sa.String(length=256), nullable=False),
        sa.Column("requested_scopes", JSONB, nullable=False, server_default="[]"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        # Single-use: set on redemption so a replayed callback is refused.
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("redirect_after", sa.String(length=512), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_oauth_states_state", "oauth_states", ["state"], unique=True)
    op.create_index("ix_oauth_states_user_id", "oauth_states", ["user_id"])
    op.create_index("ix_oauth_states_cleanup", "oauth_states", ["expires_at", "consumed_at"])

    # -------------------------------------------------------- api_usage_ledger
    op.create_table(
        "api_usage_ledger",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("x_account_id", sa.Uuid(), nullable=True),
        sa.Column("endpoint_key", sa.String(length=64), nullable=False),
        sa.Column("method", sa.String(length=8), nullable=False),
        sa.Column("cost_class", sa.String(length=16), nullable=False),
        # X bills per resource returned, not per request, so this — not the row
        # count — is what drives spend.
        sa.Column("resources_returned", sa.Integer(), nullable=False, server_default="0"),
        # Integer micro-USD (1e-6). Never a float: these are summed in bulk and
        # rounding drift in a spend ledger is unacceptable.
        sa.Column("estimated_cost_micros", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("billing_mode", sa.String(length=24), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("rate_limit_remaining", sa.Integer(), nullable=True),
        sa.Column("rate_limit_reset_at", sa.DateTime(timezone=True), nullable=True),
        # Blocked calls cost nothing but explain gaps in collection, which
        # matters because gaps in this dataset are permanent.
        sa.Column("was_blocked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("block_reason", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("context", JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["x_account_id"], ["x_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_api_usage_ledger_x_account_id", "api_usage_ledger", ["x_account_id"])
    op.create_index("ix_usage_account_time", "api_usage_ledger", ["x_account_id", "created_at"])
    op.create_index("ix_usage_endpoint_time", "api_usage_ledger", ["endpoint_key", "created_at"])


def downgrade() -> None:
    op.drop_table("api_usage_ledger")
    op.drop_table("oauth_states")
