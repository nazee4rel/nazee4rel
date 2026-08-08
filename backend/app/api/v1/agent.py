"""Agent endpoints: runs, insights, recommendations and the approval queue.

The approval endpoints are the security-sensitive ones. Both require an
authenticated user who owns the account, both write to the audit trail, and
neither can widen what an action is permitted to do — approval only satisfies
the "a human agreed" gate. Every other gate in `app.agent.executor` still
applies afterwards, which is why approving a publish action while
`X_ENABLE_WRITE_ACTIONS` is off returns a blocked action rather than a post.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select

from app.agent.loop import AgentLoop
from app.agent.policy import FORBIDDEN_ACTIONS, TIERS, PolicyViolationError, policy_for
from app.api.deps import CurrentUser, DbSession, rate_limit
from app.core.errors import NotFoundError, ValidationError
from app.models.agent import (
    ActionStatus,
    AgentAction,
    AgentRun,
    AgentRunTrigger,
    Insight,
    Recommendation,
    RecommendationStatus,
)
from app.models.revenue import Topic
from app.models.x_account import XAccount

router = APIRouter(prefix="/agent", tags=["agent"])


# ------------------------------------------------------------------- schemas
class RunOut(BaseModel):
    id: uuid.UUID
    trigger: str
    status: str
    stage: str
    started_at: datetime
    finished_at: datetime | None
    model: str | None
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    insights_created: int
    recommendations_created: int
    actions_created: int
    grounding_rejections: int
    ingested_untrusted_content: bool
    summary: str | None
    error: str | None
    stage_log: dict[str, Any]


class InsightOut(BaseModel):
    id: uuid.UUID
    agent_run_id: uuid.UUID
    kind: str
    severity: str
    title: str
    body: str
    supporting_data: dict[str, Any]
    confidence: float | None
    provenance: str
    created_at: datetime


class RecommendationOut(BaseModel):
    id: uuid.UUID
    title: str
    rationale: str
    supporting_data: dict[str, Any]
    predicted_metric: str
    predicted_direction: str
    baseline_value: float | None
    baseline_note: str | None
    verify_after: date
    status: str
    verification_result: str
    observed_value: float | None
    verification_note: str | None
    created_at: datetime


class ActionOut(BaseModel):
    id: uuid.UUID
    action_type: str
    autonomy_tier: str
    status: str
    summary: str
    payload: dict[str, Any]
    # Written by the application, never by the model: the description of what
    # you are approving must not be attacker-influenced.
    effect: str
    requires_approval: bool
    ingested_untrusted_content: bool
    expires_at: datetime | None
    executed_at: datetime | None
    result: dict[str, Any]
    error: str | None
    blocked_reason: str | None
    created_at: datetime


class DecisionIn(BaseModel):
    reason: str | None = None


class TopicIn(BaseModel):
    name: str
    description: str | None = None


class TopicOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    is_active: bool


# ------------------------------------------------------------------ helpers
async def _account(db: DbSession, user: CurrentUser, account_id: uuid.UUID) -> XAccount:
    account = await db.scalar(
        select(XAccount).where(XAccount.id == account_id, XAccount.user_id == user.id)
    )
    if account is None:
        raise NotFoundError("X account not found.")
    return account


def _action_out(action: AgentAction) -> ActionOut:
    return ActionOut(
        id=action.id,
        action_type=action.action_type.value,
        autonomy_tier=action.autonomy_tier.value,
        status=action.status.value,
        summary=action.summary,
        payload=dict(action.payload),
        effect=policy_for(action.action_type).effect,
        requires_approval=action.requires_approval,
        ingested_untrusted_content=action.ingested_untrusted_content,
        expires_at=action.expires_at,
        executed_at=action.executed_at,
        result=dict(action.result),
        error=action.error,
        blocked_reason=action.blocked_reason,
        created_at=action.created_at,
    )


# ------------------------------------------------------------------- policy
@router.get("/policy", response_model=dict)
async def autonomy_policy() -> dict[str, Any]:
    """What the agent is allowed to do. Public to any signed-in surface.

    Exposed as an endpoint so the dashboard renders the live policy rather than
    a hand-maintained copy of it that can drift from the code that enforces it.
    """
    return {
        "tiers": {
            "T0": "Runs unattended. No outward effect.",
            "T1": "Queued for your approval.",
            "T2": "Queued for your approval and visible on X once approved.",
            "T3": "Never executed. Not implemented anywhere in this system.",
        },
        "actions": [
            {
                "action_type": action.value,
                "tier": policy.tier.value,
                "effect": policy.effect,
                "outward_facing": policy.outward_facing,
                "requires_write_flag": policy.requires_write_flag,
            }
            for action, policy in TIERS.items()
        ],
        "never_implemented": list(FORBIDDEN_ACTIONS),
        "note": (
            "The agent holds no tools while reasoning. It returns structured data, and "
            "only the action types listed here can be executed — anything else fails "
            "before it reaches the executor."
        ),
    }


# --------------------------------------------------------------------- runs
@router.post(
    "/{account_id}/run",
    response_model=RunOut,
    dependencies=[Depends(rate_limit(limit=6, window_seconds=3600, scope="agent-run"))],
)
async def trigger_run(account_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Any:
    """Run a full cycle now.

    Rate-limited per hour because each run makes paid model calls and may top up
    collection. Runs inline rather than through Celery so the user sees the
    outcome — including a refusal to run — immediately.
    """
    account = await _account(db, user, account_id)
    return await AgentLoop(db, account).run(trigger=AgentRunTrigger.MANUAL)


@router.get("/{account_id}/runs", response_model=list[RunOut])
async def list_runs(
    account_id: uuid.UUID, user: CurrentUser, db: DbSession, limit: int = 25
) -> Any:
    account = await _account(db, user, account_id)
    return list(
        await db.scalars(
            select(AgentRun)
            .where(AgentRun.x_account_id == account.id)
            .order_by(AgentRun.started_at.desc())
            .limit(min(limit, 100))
        )
    )


# ----------------------------------------------------------------- insights
@router.get("/{account_id}/insights", response_model=list[InsightOut])
async def list_insights(
    account_id: uuid.UUID, user: CurrentUser, db: DbSession, limit: int = 50
) -> Any:
    account = await _account(db, user, account_id)
    return list(
        await db.scalars(
            select(Insight)
            .where(Insight.x_account_id == account.id)
            .order_by(Insight.created_at.desc())
            .limit(min(limit, 200))
        )
    )


# ---------------------------------------------------------- recommendations
@router.get("/{account_id}/recommendations", response_model=list[RecommendationOut])
async def list_recommendations(
    account_id: uuid.UUID, user: CurrentUser, db: DbSession, limit: int = 50
) -> Any:
    account = await _account(db, user, account_id)
    return list(
        await db.scalars(
            select(Recommendation)
            .where(Recommendation.x_account_id == account.id)
            .order_by(Recommendation.created_at.desc())
            .limit(min(limit, 200))
        )
    )


@router.post("/recommendations/{recommendation_id}/accept", response_model=RecommendationOut)
async def accept_recommendation(
    recommendation_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> Any:
    """Mark a recommendation as adopted.

    This matters for more than bookkeeping: the VERIFY stage will not grade
    advice that was dismissed, because what happened afterwards cannot confirm
    or refute something nobody acted on.
    """
    return await _decide(db, user, recommendation_id, RecommendationStatus.ACCEPTED)


@router.post("/recommendations/{recommendation_id}/dismiss", response_model=RecommendationOut)
async def dismiss_recommendation(
    recommendation_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> Any:
    return await _decide(db, user, recommendation_id, RecommendationStatus.DISMISSED)


async def _decide(
    db: DbSession,
    user: CurrentUser,
    recommendation_id: uuid.UUID,
    status: RecommendationStatus,
) -> Recommendation:
    recommendation = await db.scalar(
        select(Recommendation)
        .join(XAccount, XAccount.id == Recommendation.x_account_id)
        .where(Recommendation.id == recommendation_id, XAccount.user_id == user.id)
    )
    if recommendation is None:
        raise NotFoundError("Recommendation not found.")
    recommendation.status = status
    recommendation.decided_at = datetime.now(tz=None).astimezone()
    await db.flush()
    return recommendation


# ------------------------------------------------------------------ actions
@router.get("/{account_id}/actions", response_model=list[ActionOut])
async def list_actions(
    account_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    pending_only: bool = False,
    limit: int = 50,
) -> list[ActionOut]:
    account = await _account(db, user, account_id)
    stmt = select(AgentAction).where(AgentAction.x_account_id == account.id)
    if pending_only:
        stmt = stmt.where(AgentAction.status == ActionStatus.PENDING_APPROVAL)
    rows = list(
        await db.scalars(stmt.order_by(AgentAction.created_at.desc()).limit(min(limit, 200)))
    )
    return [_action_out(row) for row in rows]


@router.post("/actions/{action_id}/approve", response_model=ActionOut)
async def approve_action(
    action_id: uuid.UUID, user: CurrentUser, db: DbSession, body: DecisionIn | None = None
) -> ActionOut:
    """Approve and immediately attempt a queued action.

    Approval satisfies one gate. It does not bypass the others — an approved
    publish still needs the write flag, the granted scope and a probed
    capability, and comes back BLOCKED with the reason if any is missing.
    """
    action, executor = await _action_and_executor(db, user, action_id)
    try:
        updated = await executor.approve(action, user)
    except PolicyViolationError as exc:
        raise ValidationError(str(exc)) from exc
    return _action_out(updated)


@router.post("/actions/{action_id}/reject", response_model=ActionOut)
async def reject_action(
    action_id: uuid.UUID, user: CurrentUser, db: DbSession, body: DecisionIn | None = None
) -> ActionOut:
    action, executor = await _action_and_executor(db, user, action_id)
    try:
        updated = await executor.reject(action, user, body.reason if body else None)
    except PolicyViolationError as exc:
        raise ValidationError(str(exc)) from exc
    return _action_out(updated)


async def _action_and_executor(
    db: DbSession, user: CurrentUser, action_id: uuid.UUID
) -> tuple[AgentAction, Any]:
    from app.agent.executor import ActionExecutor

    action = await db.scalar(
        select(AgentAction)
        .join(XAccount, XAccount.id == AgentAction.x_account_id)
        .where(AgentAction.id == action_id, XAccount.user_id == user.id)
    )
    if action is None:
        raise NotFoundError("Action not found.")
    account = await db.get(XAccount, action.x_account_id)
    if account is None:  # pragma: no cover — guarded by the join above
        raise NotFoundError("X account not found.")
    return action, ActionExecutor(db, account)


# ------------------------------------------------------------------- topics
@router.get("/{account_id}/topics", response_model=list[TopicOut])
async def list_topics(account_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Any:
    account = await _account(db, user, account_id)
    return list(
        await db.scalars(select(Topic).where(Topic.x_account_id == account.id).order_by(Topic.name))
    )


@router.post("/{account_id}/topics", response_model=TopicOut)
async def create_topic(
    account_id: uuid.UUID, body: TopicIn, user: CurrentUser, db: DbSession
) -> Any:
    """Add a topic yourself.

    The taxonomy is meant to be yours. The agent may only assign from it and
    suggest additions; you are the one who changes it.
    """
    account = await _account(db, user, account_id)
    name = body.name.strip()
    if not name:
        raise ValidationError("A topic needs a name.")
    existing = await db.scalar(
        select(Topic).where(Topic.x_account_id == account.id, Topic.name == name)
    )
    if existing is not None:
        return existing
    topic = Topic(x_account_id=account.id, name=name[:80], description=body.description)
    db.add(topic)
    await db.flush()
    return topic
