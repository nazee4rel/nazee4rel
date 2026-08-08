"""Phase 2: identity, access, connected X accounts, capability matrix, audit.

Content and metric tables arrive in Phases 3-4; keeping them out of this
migration means Phase 2 is independently deployable and reversible.

Revision ID: 0001_identity_and_access
Revises:
Create Date: 2026-08-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_identity_and_access"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    # ------------------------------------------------------------------ users
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column(
            "role",
            sa.Enum("OWNER", "ADMIN", "VIEWER", name="user_role"),
            nullable=False,
            server_default="OWNER",
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC"),
        sa.Column("totp_secret_enc", sa.String(length=512), nullable=True),
        sa.Column("totp_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_active", "users", ["is_active", "deleted_at"])

    # --------------------------------------------------------------- sessions
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        # SHA-256 digest only — a database leak must not yield live sessions.
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sessions_token_hash", "sessions", ["token_hash"], unique=True)
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_user_active", "sessions", ["user_id", "revoked_at", "expires_at"])

    # ------------------------------------------------------------- x_accounts
    op.create_table(
        "x_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        # Numeric X id, not the handle — handles change, ids do not.
        sa.Column("x_user_id", sa.String(length=32), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("profile_image_url", sa.String(length=512), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
        # Boundary of the impression dataset — nothing before this can be
        # backfilled once posts pass the API's 30-day non-public-metrics window.
        sa.Column("collection_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "x_user_id", name="uq_x_accounts_user_xuser"),
    )
    op.create_index("ix_x_accounts_user_id", "x_accounts", ["user_id"])
    op.create_index("ix_x_accounts_active", "x_accounts", ["user_id", "is_active"])

    # ------------------------------------------------------------ oauth_tokens
    op.create_table(
        "oauth_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("x_account_id", sa.Uuid(), nullable=False),
        # AES-256-GCM ciphertext. Never returned by any API schema.
        sa.Column("access_token_enc", sa.Text(), nullable=False),
        sa.Column("refresh_token_enc", sa.Text(), nullable=True),
        sa.Column("scopes", JSONB, nullable=False, server_default="[]"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["x_account_id"], ["x_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_oauth_tokens_x_account_id", "oauth_tokens", ["x_account_id"], unique=True)

    # ----------------------------------------------------- account_capabilities
    op.create_table(
        "account_capabilities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("x_account_id", sa.Uuid(), nullable=False),
        sa.Column(
            "capability",
            sa.Enum(
                "READ_OWN_PROFILE",
                "READ_OWN_POSTS",
                "READ_PUBLIC_METRICS",
                "READ_NON_PUBLIC_METRICS",
                "READ_ORGANIC_METRICS",
                "READ_FOLLOWERS_LIST",
                "READ_LIKED_POSTS",
                "READ_BOOKMARKS",
                "WRITE_POSTS",
                name="x_capability",
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "UNKNOWN",
                "AVAILABLE",
                "UNAVAILABLE",
                "FORBIDDEN",
                "ERROR",
                name="capability_status",
            ),
            nullable=False,
            server_default="UNKNOWN",
        ),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("details", JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["x_account_id"], ["x_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("x_account_id", "capability", name="uq_capability_per_account"),
    )
    op.create_index(
        "ix_account_capabilities_x_account_id", "account_capabilities", ["x_account_id"]
    )
    op.create_index("ix_capabilities_status", "account_capabilities", ["x_account_id", "status"])

    # ------------------------------------------------------------- audit_logs
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("x_account_id", sa.Uuid(), nullable=True),
        sa.Column(
            "action",
            sa.Enum(
                "LOGIN_SUCCEEDED",
                "LOGIN_FAILED",
                "LOGOUT",
                "USER_CREATED",
                "PASSWORD_CHANGED",
                "SESSION_REVOKED",
                "X_ACCOUNT_CONNECTED",
                "X_ACCOUNT_DISCONNECTED",
                "X_TOKEN_REFRESHED",
                "CAPABILITY_PROBE_RUN",
                "AGENT_ACTION_APPROVED",
                "AGENT_ACTION_REJECTED",
                "AGENT_ACTION_EXECUTED",
                "SETTINGS_CHANGED",
                "BUDGET_CEILING_CHANGED",
                name="audit_action",
            ),
            nullable=False,
        ),
        sa.Column("target_type", sa.String(length=64), nullable=True),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("context", JSONB, nullable=False, server_default="{}"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # SET NULL rather than CASCADE: the audit trail must outlive the actor.
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["x_account_id"], ["x_accounts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_user_id", "audit_logs", ["user_id"])
    op.create_index("ix_audit_logs_x_account_id", "audit_logs", ["x_account_id"])
    op.create_index("ix_audit_user_time", "audit_logs", ["user_id", "created_at"])
    op.create_index("ix_audit_action_time", "audit_logs", ["action", "created_at"])

    # ------------------------------------------------------------ system_logs
    op.create_table(
        "system_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("x_account_id", sa.Uuid(), nullable=True),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("component", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
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
    op.create_index("ix_system_logs_x_account_id", "system_logs", ["x_account_id"])
    op.create_index("ix_system_logs_lookup", "system_logs", ["x_account_id", "level", "created_at"])


def downgrade() -> None:
    op.drop_table("system_logs")
    op.drop_table("audit_logs")
    op.drop_table("account_capabilities")
    op.drop_table("oauth_tokens")
    op.drop_table("x_accounts")
    op.drop_table("sessions")
    op.drop_table("users")

    # Postgres keeps enum types after their tables are gone.
    for enum_name in (
        "audit_action",
        "capability_status",
        "x_capability",
        "user_role",
    ):
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
