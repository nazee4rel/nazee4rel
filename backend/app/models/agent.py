"""The agent's own record: runs, insights, recommendations and actions.

Four ideas are enforced by this schema rather than by convention.

**A run is auditable.** `agent_runs.evidence` stores the exact fact bundle the
model was shown. An insight can therefore be re-checked against its inputs
months later, which is the only way to tell a good call from a lucky one.

**An insight cites its figures.** `supporting_data` holds the fact keys and
values the model quoted, and those are verified against the evidence bundle
before the row is written. An insight that cites a figure the evidence does not
contain is not stored.

**A recommendation is falsifiable.** It carries a predicted metric, direction,
baseline and a date after which it can be graded. Advice that cannot be wrong
cannot be learned from.

**An action is gated.** Every action declares an autonomy tier, and the tier —
not the model's confidence — decides whether it runs unattended, waits for
approval, or is refused outright.
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
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import Provenance

if TYPE_CHECKING:
    from app.models.user import User

JSONType = JSON().with_variant(JSONB(), "postgresql")


class AgentStage(enum.StrEnum):
    """The eight stages of one cycle, in order.

    Stored per run so a failure is attributable to a stage rather than to "the
    agent". VERIFY grades *previous* runs' recommendations; the grades reach the
    model on the following cycle, which keeps the loop honest — nothing is
    graded by the same call that proposed it.
    """

    OBSERVE = "OBSERVE"
    COLLECT = "COLLECT"
    ANALYZE = "ANALYZE"
    REASON = "REASON"
    RECOMMEND = "RECOMMEND"
    ACT = "ACT"
    VERIFY = "VERIFY"
    REPORT = "REPORT"


class AgentRunStatus(enum.StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    # Finished, but some stage was degraded — e.g. reasoning was skipped
    # because the model was unreachable. Distinct from FAILED so a partial
    # cycle still shows its analytics.
    PARTIAL = "PARTIAL"
    # Deliberately did nothing: not enough data to reason over. Not an error.
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class AgentRunTrigger(enum.StrEnum):
    SCHEDULED = "SCHEDULED"
    MANUAL = "MANUAL"
    ALERT = "ALERT"


class InsightKind(enum.StrEnum):
    GROWTH = "GROWTH"
    ENGAGEMENT = "ENGAGEMENT"
    CONTENT = "CONTENT"
    TIMING = "TIMING"
    REVENUE = "REVENUE"
    RISK = "RISK"
    DATA_QUALITY = "DATA_QUALITY"


class InsightSeverity(enum.StrEnum):
    INFO = "INFO"
    NOTABLE = "NOTABLE"
    URGENT = "URGENT"


class RecommendationStatus(enum.StrEnum):
    PROPOSED = "PROPOSED"
    ACCEPTED = "ACCEPTED"
    DISMISSED = "DISMISSED"
    SUPERSEDED = "SUPERSEDED"


class PredictedMetric(enum.StrEnum):
    """Metrics the VERIFY stage knows how to measure.

    Restricted on purpose: a prediction about something the system cannot
    measure is unfalsifiable, so the structured output only offers these.
    """

    FOLLOWER_GROWTH_DAILY = "FOLLOWER_GROWTH_DAILY"
    ENGAGEMENT_RATE_MEDIAN = "ENGAGEMENT_RATE_MEDIAN"
    POSTS_PER_WEEK = "POSTS_PER_WEEK"
    IMPRESSIONS_MEDIAN = "IMPRESSIONS_MEDIAN"


class PredictedDirection(enum.StrEnum):
    INCREASE = "INCREASE"
    DECREASE = "DECREASE"
    MAINTAIN = "MAINTAIN"


class VerificationResult(enum.StrEnum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    REFUTED = "REFUTED"
    # Includes "you never adopted it" — advice nobody followed cannot be
    # scored either way, and pretending otherwise would corrupt the feedback.
    INCONCLUSIVE = "INCONCLUSIVE"


class ActionType(enum.StrEnum):
    """The complete allowlist of things the agent may ever do.

    The executor refuses anything not on this list, so a model that invents an
    action name fails closed. Nothing here deletes, edits or spends.
    """

    RECORD_NOTE = "RECORD_NOTE"
    RAISE_ALERT = "RAISE_ALERT"
    DRAFT_POST = "DRAFT_POST"
    PROPOSE_TOPIC = "PROPOSE_TOPIC"
    PUBLISH_POST = "PUBLISH_POST"


class AutonomyTier(enum.StrEnum):
    """How much independence an action type carries.

    T3 is not a permission the user can grant here — it names the class of
    action this system refuses to implement at all (deleting posts, changing
    account settings, spending money). It exists so the boundary is written
    down rather than merely absent.
    """

    T0 = "T0"  # runs unattended; no outward effect
    T1 = "T1"  # queued for approval
    T2 = "T2"  # queued for approval and outward-facing; extra confirmation
    T3 = "T3"  # never executed


class ActionStatus(enum.StrEnum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    # Approval windows lapse. An action approved days later would act on stale
    # analysis, so pending actions expire instead of waiting indefinitely.
    EXPIRED = "EXPIRED"
    # Refused by a guard: tier, missing capability, or the write flag being off.
    BLOCKED = "BLOCKED"


class AgentRun(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "agent_runs"

    x_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("x_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    trigger: Mapped[AgentRunTrigger] = mapped_column(
        Enum(AgentRunTrigger, name="agent_run_trigger"),
        default=AgentRunTrigger.SCHEDULED,
        nullable=False,
    )
    status: Mapped[AgentRunStatus] = mapped_column(
        Enum(AgentRunStatus, name="agent_run_status"),
        default=AgentRunStatus.RUNNING,
        nullable=False,
    )
    # Where the run got to. On failure this is the stage that failed.
    stage: Mapped[AgentStage] = mapped_column(
        Enum(AgentStage, name="agent_stage"), default=AgentStage.OBSERVE, nullable=False
    )

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # Per-stage outcome and duration: {"ANALYZE": {"ms": 812, "note": "..."}}.
    stage_log: Mapped[dict[str, object]] = mapped_column(JSONType, default=dict, nullable=False)

    # Exactly what the model was shown. Without this an insight cannot be
    # audited after the fact, and grounding checks have nothing to check
    # against.
    evidence: Mapped[dict[str, object]] = mapped_column(JSONType, default=dict, nullable=False)

    model: Mapped[str | None] = mapped_column(String(80), default=None)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # True when any third-party text (replies, quote posts) entered the prompt.
    # Everything produced by such a run is treated as potentially influenced by
    # an attacker: no action from it executes unattended.
    ingested_untrusted_content: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    insights_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    recommendations_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    actions_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Insights the model produced that cited figures absent from the evidence,
    # and were therefore discarded. A rising count is a real signal.
    grounding_rejections: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    summary: Mapped[str | None] = mapped_column(Text, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)

    insights: Mapped[list[Insight]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    recommendations: Mapped[list[Recommendation]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_agent_runs_account_started", "x_account_id", "started_at"),)

    def __repr__(self) -> str:
        return f"<AgentRun {self.status.value} at={self.started_at:%Y-%m-%d %H:%M}>"


class Insight(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One grounded observation about the account.

    `provenance` here describes the *finding*, not the underlying metric: an
    insight is INFERRED when it interprets a model output and DERIVED when it
    restates arithmetic on measured values.
    """

    __tablename__ = "insights"

    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    x_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("x_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )

    kind: Mapped[InsightKind] = mapped_column(
        Enum(InsightKind, name="insight_kind"), nullable=False
    )
    severity: Mapped[InsightSeverity] = mapped_column(
        Enum(InsightSeverity, name="insight_severity"),
        default=InsightSeverity.INFO,
        nullable=False,
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    # The figures quoted, as {fact_key: value}. Verified against the run's
    # evidence bundle before this row is written.
    supporting_data: Mapped[dict[str, object]] = mapped_column(
        JSONType, default=dict, nullable=False
    )

    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), default=None)
    provenance: Mapped[Provenance] = mapped_column(
        Enum(Provenance, name="provenance"), default=Provenance.INFERRED, nullable=False
    )

    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    run: Mapped[AgentRun] = relationship(back_populates="insights")

    __table_args__ = (
        Index("ix_insights_account_created", "x_account_id", "created_at"),
        Index("ix_insights_account_kind", "x_account_id", "kind"),
    )

    def __repr__(self) -> str:
        return f"<Insight {self.kind.value} {self.title[:40]!r}>"


class Recommendation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Advice stored as a prediction that can later be shown wrong."""

    __tablename__ = "recommendations"

    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    x_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("x_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    supporting_data: Mapped[dict[str, object]] = mapped_column(
        JSONType, default=dict, nullable=False
    )

    # --- the falsifiable part ----------------------------------------------
    predicted_metric: Mapped[PredictedMetric] = mapped_column(
        Enum(PredictedMetric, name="predicted_metric"), nullable=False
    )
    predicted_direction: Mapped[PredictedDirection] = mapped_column(
        Enum(PredictedDirection, name="predicted_direction"), nullable=False
    )
    # Measured at proposal time, not asserted by the model. Comparing against a
    # model-supplied baseline would let it grade itself.
    baseline_value: Mapped[float | None] = mapped_column(Float, default=None)
    baseline_note: Mapped[str | None] = mapped_column(Text, default=None)
    verify_after: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    status: Mapped[RecommendationStatus] = mapped_column(
        Enum(RecommendationStatus, name="recommendation_status"),
        default=RecommendationStatus.PROPOSED,
        nullable=False,
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    verification_result: Mapped[VerificationResult] = mapped_column(
        Enum(VerificationResult, name="verification_result"),
        default=VerificationResult.PENDING,
        nullable=False,
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    observed_value: Mapped[float | None] = mapped_column(Float, default=None)
    verification_note: Mapped[str | None] = mapped_column(Text, default=None)

    provenance: Mapped[Provenance] = mapped_column(
        Enum(Provenance, name="provenance"), default=Provenance.INFERRED, nullable=False
    )

    run: Mapped[AgentRun] = relationship(back_populates="recommendations")
    actions: Mapped[list[AgentAction]] = relationship(back_populates="recommendation")

    __table_args__ = (
        Index("ix_recommendations_account_status", "x_account_id", "status"),
        # Drives the VERIFY stage's "what is due for grading" query.
        Index("ix_recommendations_due", "verification_result", "verify_after"),
    )

    def __repr__(self) -> str:
        return f"<Recommendation {self.title[:40]!r} verify_after={self.verify_after}>"


class AgentAction(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A concrete, executable step — proposed, gated, then recorded.

    Rows are never deleted. A rejected or blocked action is as much a part of
    the audit trail as an executed one, and is more informative.
    """

    __tablename__ = "agent_actions"

    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), default=None, index=True
    )
    x_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("x_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recommendation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("recommendations.id", ondelete="SET NULL"), default=None, index=True
    )

    action_type: Mapped[ActionType] = mapped_column(
        Enum(ActionType, name="agent_action_type"), nullable=False
    )
    autonomy_tier: Mapped[AutonomyTier] = mapped_column(
        Enum(AutonomyTier, name="autonomy_tier"), nullable=False
    )
    status: Mapped[ActionStatus] = mapped_column(
        Enum(ActionStatus, name="agent_action_status"),
        default=ActionStatus.PENDING_APPROVAL,
        nullable=False,
    )

    # Validated against the action type's schema before execution. The model
    # fills this in; nothing in it is ever interpreted as an instruction.
    payload: Mapped[dict[str, object]] = mapped_column(JSONType, default=dict, nullable=False)
    summary: Mapped[str] = mapped_column(String(300), nullable=False, default="")

    requires_approval: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, index=True
    )

    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    result: Mapped[dict[str, object]] = mapped_column(JSONType, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    blocked_reason: Mapped[str | None] = mapped_column(Text, default=None)

    # Carried down from the run. An action from a run that read third-party
    # text can never execute unattended, whatever its tier would otherwise
    # allow.
    ingested_untrusted_content: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    recommendation: Mapped[Recommendation | None] = relationship(back_populates="actions")
    approved_by: Mapped[User | None] = relationship()

    __table_args__ = (
        Index("ix_agent_actions_account_status", "x_account_id", "status"),
        Index("ix_agent_actions_pending", "status", "expires_at"),
    )

    @property
    def is_pending(self) -> bool:
        return self.status is ActionStatus.PENDING_APPROVAL

    def __repr__(self) -> str:
        return f"<AgentAction {self.action_type.value} {self.status.value}>"
