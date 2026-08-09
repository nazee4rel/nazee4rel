"""Alerts, reports and their delivery record.

The design problem in this phase is not detection — the analytics engine
already finds anomalies. It is **alert fatigue**. A system that fires every day
gets muted, and a muted system is worse than none, because the one alert that
mattered arrives into a channel nobody reads any more. So three things are in
the schema rather than left to the detectors:

* `dedupe_key` — the identity of the *event*, not the check. The same viral post
  crossing the threshold on four consecutive runs is one alert.
* `cooldown_until` — a per-rule quiet period, so a noisy week produces a
  handful of alerts rather than a hundred.
* `is_stateful` rules resolve themselves. "Collection has stopped" is a
  condition that ends; "you gained 400 followers on Tuesday" is a moment that
  does not. Conflating them produces either a stuck banner or an unclosable one.

Reports are stored rather than only sent. An emailed report that bounced, or
that was sent before the account owner configured SMTP, still exists and is
readable in the dashboard.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
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
    from app.models.user import User

JSONType = JSON().with_variant(JSONB(), "postgresql")


class AlertRuleKey(enum.StrEnum):
    """Every condition this system watches for.

    Deliberately a closed set. An alert type that is not here cannot be raised,
    which keeps "what will wake me up?" answerable by reading one enum.
    """

    # --- statistical: point-in-time observations about the account ---------
    FOLLOWER_SPIKE = "FOLLOWER_SPIKE"
    FOLLOWER_DROP = "FOLLOWER_DROP"
    BREAKOUT_POST = "BREAKOUT_POST"
    ENGAGEMENT_DECLINE = "ENGAGEMENT_DECLINE"
    REVENUE_CHANGE = "REVENUE_CHANGE"
    UNUSUAL_CHURN = "UNUSUAL_CHURN"

    # --- operational: conditions that start and later end ------------------
    COLLECTION_STALLED = "COLLECTION_STALLED"
    FREEZE_AT_RISK = "FREEZE_AT_RISK"
    BUDGET_PRESSURE = "BUDGET_PRESSURE"
    ACCESS_DEGRADED = "ACCESS_DEGRADED"


class AlertSeverity(enum.StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    # Reserved for things that lose data or stop the system. Impressions inside
    # the 30-day window cannot be re-fetched, so anything threatening them
    # qualifies; a bad engagement week does not.
    CRITICAL = "CRITICAL"


class AlertState(enum.StrEnum):
    FIRING = "FIRING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    # Only ever reached by stateful rules, and only by the detector observing
    # that the condition has cleared — never by a human clicking something.
    RESOLVED = "RESOLVED"


class ReportPeriod(enum.StrEnum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"


class ReportStatus(enum.StrEnum):
    GENERATED = "GENERATED"
    DELIVERED = "DELIVERED"
    # The report exists and is readable in the dashboard even when sending it
    # failed. Delivery is a separate concern from generation.
    DELIVERY_FAILED = "DELIVERY_FAILED"


class DeliveryChannel(enum.StrEnum):
    DASHBOARD = "DASHBOARD"
    EMAIL = "EMAIL"


class DeliveryStatus(enum.StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    # Channel not configured. Distinct from FAILED: nothing went wrong.
    SKIPPED = "SKIPPED"


class AlertRule(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Per-account configuration for one rule.

    Rows are created lazily with defaults from the rule registry, so a new rule
    starts working without a migration and without the user configuring it.
    """

    __tablename__ = "alert_rules"

    x_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("x_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rule_key: Mapped[AlertRuleKey] = mapped_column(
        Enum(AlertRuleKey, name="alert_rule_key"), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Quiet period after this rule fires. The single most effective control
    # against fatigue, and the one most worth making adjustable.
    cooldown_hours: Mapped[int] = mapped_column(Integer, default=24, nullable=False)
    # Findings below this are recorded but not delivered.
    min_severity: Mapped[AlertSeverity] = mapped_column(
        Enum(AlertSeverity, name="alert_severity"), default=AlertSeverity.INFO, nullable=False
    )
    # Rule-specific numeric overrides, e.g. {"z_threshold": 4.0}.
    thresholds: Mapped[dict[str, object]] = mapped_column(JSONType, default=dict, nullable=False)
    channels: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)

    __table_args__ = (
        UniqueConstraint("x_account_id", "rule_key", name="uq_alert_rule_account_key"),
    )

    def __repr__(self) -> str:
        return f"<AlertRule {self.rule_key.value} enabled={self.enabled}>"


class Alert(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "alerts"

    x_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("x_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rule_key: Mapped[AlertRuleKey] = mapped_column(
        Enum(AlertRuleKey, name="alert_rule_key"), nullable=False
    )
    severity: Mapped[AlertSeverity] = mapped_column(
        Enum(AlertSeverity, name="alert_severity"), nullable=False
    )
    state: Mapped[AlertState] = mapped_column(
        Enum(AlertState, name="alert_state"), default=AlertState.FIRING, nullable=False
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # The figures behind the alert, so it can be checked rather than trusted.
    facts: Mapped[dict[str, object]] = mapped_column(JSONType, default=dict, nullable=False)
    provenance: Mapped[Provenance] = mapped_column(
        Enum(Provenance, name="provenance"), default=Provenance.DERIVED, nullable=False
    )

    # Identity of the underlying event. Unique per account, so the same viral
    # post crossing the threshold on four consecutive runs is one alert.
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    fired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    acknowledged_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    acknowledged_by: Mapped[User | None] = relationship()
    deliveries: Mapped[list[Delivery]] = relationship(
        back_populates="alert", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("x_account_id", "dedupe_key", name="uq_alert_dedupe"),
        Index("ix_alerts_account_state", "x_account_id", "state"),
        Index("ix_alerts_account_fired", "x_account_id", "fired_at"),
    )

    @property
    def is_open(self) -> bool:
        return self.state is AlertState.FIRING

    def __repr__(self) -> str:
        return f"<Alert {self.rule_key.value} {self.severity.value} {self.state.value}>"


class Report(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A period summary, stored whether or not it was successfully delivered."""

    __tablename__ = "reports"

    x_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("x_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    period: Mapped[ReportPeriod] = mapped_column(
        Enum(ReportPeriod, name="report_period"), nullable=False
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Ordered list of {key, title, body, facts, provenance, caveats}. Stored
    # structured rather than as rendered text so the same report can be shown
    # in the dashboard and emailed without being generated twice.
    sections: Mapped[list[dict[str, object]]] = mapped_column(
        JSONType, default=list, nullable=False
    )

    status: Mapped[ReportStatus] = mapped_column(
        Enum(ReportStatus, name="report_status"), default=ReportStatus.GENERATED, nullable=False
    )

    deliveries: Mapped[list[Delivery]] = relationship(
        back_populates="report", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # One report per account per period. Re-running the job amends rather
        # than duplicating.
        UniqueConstraint("x_account_id", "period", "period_start", name="uq_report_period"),
        Index("ix_reports_account_generated", "x_account_id", "generated_at"),
    )

    def __repr__(self) -> str:
        return f"<Report {self.period.value} {self.period_start}>"


class Delivery(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One attempt to put an alert or a report in front of a human.

    Kept separate from the alert so a failed email does not mean a lost alert,
    and so "why didn't I hear about this?" has an answer with a timestamp.
    """

    __tablename__ = "deliveries"

    alert_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("alerts.id", ondelete="CASCADE"), default=None, index=True
    )
    report_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("reports.id", ondelete="CASCADE"), default=None, index=True
    )

    channel: Mapped[DeliveryChannel] = mapped_column(
        Enum(DeliveryChannel, name="delivery_channel"), nullable=False
    )
    status: Mapped[DeliveryStatus] = mapped_column(
        Enum(DeliveryStatus, name="delivery_status"),
        default=DeliveryStatus.PENDING,
        nullable=False,
    )
    # Recipient, redacted to a domain-preserving form. Never a credential.
    target: Mapped[str | None] = mapped_column(String(200), default=None)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)

    alert: Mapped[Alert | None] = relationship(back_populates="deliveries")
    report: Mapped[Report | None] = relationship(back_populates="deliveries")

    __table_args__ = (Index("ix_deliveries_channel_status", "channel", "status"),)

    def __repr__(self) -> str:
        return f"<Delivery {self.channel.value} {self.status.value}>"
