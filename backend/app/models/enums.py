"""Enumerations shared across the schema.

`Provenance` is the most important type in this codebase. Section 2 of the
architecture doc requires that every metric-bearing row declares where its value
came from, so that "don't invent data" is enforced by the schema rather than by
discipline. It is defined here in Phase 2 — before the tables that use it exist
— so that no later model can be written without it being available.
"""

from __future__ import annotations

import enum


class Provenance(enum.StrEnum):
    """Where a value came from. Non-nullable on every metric-bearing row."""

    MEASURED = "MEASURED"  # returned directly by the X API
    DERIVED = "DERIVED"  # deterministic arithmetic on measured values
    INFERRED = "INFERRED"  # statistical model output; needs a confidence interval
    USER_ENTERED = "USER_ENTERED"  # typed in by a human
    IMPORTED = "IMPORTED"  # from CSV or a third-party API such as Stripe
    UNAVAILABLE = "UNAVAILABLE"  # cannot be obtained — renders as a gap, never as 0

    @property
    def is_factual(self) -> bool:
        """True only for values X actually reported or that follow from them."""
        return self in (Provenance.MEASURED, Provenance.DERIVED)


class UserRole(enum.StrEnum):
    OWNER = "OWNER"  # full control including agent autonomy settings
    ADMIN = "ADMIN"  # may approve agent actions
    VIEWER = "VIEWER"  # read-only


class CapabilityStatus(enum.StrEnum):
    """Result of probing one X API capability.

    UNKNOWN is the initial state and is meaningful: the UI shows "not yet
    checked" rather than assuming either availability or absence.
    """

    UNKNOWN = "UNKNOWN"
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"  # endpoint exists but access level forbids it
    FORBIDDEN = "FORBIDDEN"  # 403 — scope or tier problem
    ERROR = "ERROR"  # probe itself failed; retry


class XCapability(enum.StrEnum):
    """Capabilities probed at connect-time and weekly thereafter.

    Section 1.5: several of these are documented inconsistently or have changed
    access rules recently, so the system asks rather than assumes.
    """

    READ_OWN_PROFILE = "READ_OWN_PROFILE"
    READ_OWN_POSTS = "READ_OWN_POSTS"
    READ_PUBLIC_METRICS = "READ_PUBLIC_METRICS"
    READ_NON_PUBLIC_METRICS = "READ_NON_PUBLIC_METRICS"
    READ_ORGANIC_METRICS = "READ_ORGANIC_METRICS"
    READ_FOLLOWERS_LIST = "READ_FOLLOWERS_LIST"  # removed from some tiers
    READ_LIKED_POSTS = "READ_LIKED_POSTS"
    READ_BOOKMARKS = "READ_BOOKMARKS"
    WRITE_POSTS = "WRITE_POSTS"  # only if X_ENABLE_WRITE_ACTIONS


class AuditAction(enum.StrEnum):
    LOGIN_SUCCEEDED = "LOGIN_SUCCEEDED"
    LOGIN_FAILED = "LOGIN_FAILED"
    LOGOUT = "LOGOUT"
    USER_CREATED = "USER_CREATED"
    PASSWORD_CHANGED = "PASSWORD_CHANGED"
    SESSION_REVOKED = "SESSION_REVOKED"
    X_ACCOUNT_CONNECTED = "X_ACCOUNT_CONNECTED"
    X_ACCOUNT_DISCONNECTED = "X_ACCOUNT_DISCONNECTED"
    X_TOKEN_REFRESHED = "X_TOKEN_REFRESHED"
    CAPABILITY_PROBE_RUN = "CAPABILITY_PROBE_RUN"
    AGENT_ACTION_APPROVED = "AGENT_ACTION_APPROVED"
    AGENT_ACTION_REJECTED = "AGENT_ACTION_REJECTED"
    AGENT_ACTION_EXECUTED = "AGENT_ACTION_EXECUTED"
    SETTINGS_CHANGED = "SETTINGS_CHANGED"
    BUDGET_CEILING_CHANGED = "BUDGET_CEILING_CHANGED"
