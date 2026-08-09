"""The only code path that turns a model's output into an effect.

Nothing the model produces reaches this module as text. A proposal arrives as an
`ActionType` enum member and a payload dict, and every one of them passes the
same seven gates before anything happens:

1. The action type must be declared in `app.agent.policy`. An undeclared name
   raises rather than falling through to a default.
2. T3 is refused unconditionally.
3. Anything above T0 must carry an approval row from a real user.
4. A lapsed approval window expires the action instead of executing it late.
5. Outward-facing actions require `X_ENABLE_WRITE_ACTIONS`. With the flag off
   the application never requests `tweet.write`, so this gate is the second
   lock on a door that has no key cut for it.
6. The capability the action needs must be probed as AVAILABLE.
7. The payload must validate against that action type's schema.

Failures are recorded, never raised past this boundary: a blocked action is a
row in the audit trail with a reason, which is more useful than an exception
that stops the rest of the cycle.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.policy import (
    APPROVAL_TTL_HOURS,
    PolicyViolationError,
    policy_for,
    validate_payload,
)
from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.agent import (
    ActionStatus,
    ActionType,
    AgentAction,
    AutonomyTier,
)
from app.models.enums import AuditAction, CapabilityStatus
from app.models.revenue import Topic
from app.models.user import User
from app.models.x_account import AccountCapability, XAccount
from app.services.audit_service import AuditService

log = get_logger(__name__)


class ActionExecutor:
    def __init__(self, db: AsyncSession, account: XAccount) -> None:
        self.db = db
        self.account = account
        self.audit = AuditService(db)
        self.settings = get_settings()

    # ------------------------------------------------------------- proposing
    async def propose(
        self,
        action_type: ActionType | str,
        payload: dict[str, Any],
        *,
        summary: str,
        agent_run_id: uuid.UUID | None = None,
        recommendation_id: uuid.UUID | None = None,
        ingested_untrusted_content: bool = False,
    ) -> AgentAction:
        """Record a proposed action and, when the tier allows, run it now.

        Even a rejected proposal produces a row. An action the agent wanted to
        take and was refused is part of the record — arguably the more
        interesting part.
        """
        try:
            policy = policy_for(action_type)
            resolved = (
                action_type if isinstance(action_type, ActionType) else ActionType(action_type)
            )
        except (PolicyViolationError, ValueError) as exc:
            # Nothing to store against an action type that does not exist, so
            # this is logged rather than persisted as an AgentAction row.
            log.warning("agent.action_undeclared", proposed=str(action_type), error=str(exc))
            await self.audit.system(
                "WARNING",
                "agent.executor",
                f"Refused an undeclared action type: {action_type!r}",
                x_account_id=self.account.id,
            )
            raise PolicyViolationError(str(exc)) from exc

        needs_approval = policy.tier is not AutonomyTier.T0
        action = AgentAction(
            agent_run_id=agent_run_id,
            x_account_id=self.account.id,
            recommendation_id=recommendation_id,
            action_type=resolved,
            autonomy_tier=policy.tier,
            status=ActionStatus.PENDING_APPROVAL if needs_approval else ActionStatus.APPROVED,
            payload=dict(payload),
            summary=summary[:300],
            requires_approval=needs_approval,
            expires_at=datetime.now(UTC) + timedelta(hours=APPROVAL_TTL_HOURS)
            if needs_approval
            else None,
            ingested_untrusted_content=ingested_untrusted_content,
        )
        self.db.add(action)
        await self.db.flush()

        if not needs_approval:
            await self.execute(action)
        return action

    # ------------------------------------------------------------- approving
    async def approve(self, action: AgentAction, user: User) -> AgentAction:
        if action.status is not ActionStatus.PENDING_APPROVAL:
            raise PolicyViolationError(
                f"This action is {action.status.value}, so it cannot be approved."
            )
        if action.expires_at is not None and _aware(action.expires_at) <= datetime.now(UTC):
            action.status = ActionStatus.EXPIRED
            action.blocked_reason = (
                "The approval window lapsed. Approving stale analysis would act on "
                "figures that have since changed, so the action was expired instead."
            )
            await self.db.flush()
            return action

        action.status = ActionStatus.APPROVED
        action.approved_by_user_id = user.id
        action.approved_at = datetime.now(UTC)
        await self.audit.record(
            AuditAction.AGENT_ACTION_APPROVED,
            user_id=user.id,
            x_account_id=self.account.id,
            target_type="agent_action",
            target_id=str(action.id),
            note=action.summary,
            action_type=action.action_type.value,
            tier=action.autonomy_tier.value,
            ingested_untrusted_content=action.ingested_untrusted_content,
        )
        await self.db.flush()
        return await self.execute(action)

    async def reject(
        self, action: AgentAction, user: User, reason: str | None = None
    ) -> AgentAction:
        if action.status not in (ActionStatus.PENDING_APPROVAL, ActionStatus.APPROVED):
            raise PolicyViolationError(
                f"This action is {action.status.value} and cannot be rejected."
            )
        action.status = ActionStatus.REJECTED
        action.blocked_reason = reason or "Rejected by the account owner."
        await self.audit.record(
            AuditAction.AGENT_ACTION_REJECTED,
            user_id=user.id,
            x_account_id=self.account.id,
            target_type="agent_action",
            target_id=str(action.id),
            note=reason,
            action_type=action.action_type.value,
        )
        await self.db.flush()
        return action

    # ------------------------------------------------------------- executing
    async def execute(self, action: AgentAction) -> AgentAction:
        """Run an action through every gate, then perform it."""
        blocked = await self._gate(action)
        if blocked is not None:
            action.status = (
                ActionStatus.EXPIRED if blocked.startswith("expired:") else ActionStatus.BLOCKED
            )
            action.blocked_reason = blocked.removeprefix("expired:").strip()
            await self.db.flush()
            log.info(
                "agent.action_blocked",
                action=action.action_type.value,
                reason=action.blocked_reason,
            )
            return action

        try:
            clean_payload = validate_payload(action.action_type, action.payload)
            result = await self._perform(action, clean_payload)
        except PolicyViolationError as exc:
            action.status = ActionStatus.BLOCKED
            action.blocked_reason = str(exc)
            await self.db.flush()
            return action
        except Exception as exc:  # noqa: BLE001 — one bad action must not stop the cycle
            action.status = ActionStatus.FAILED
            action.error = str(exc)[:1000]
            await self.db.flush()
            log.exception("agent.action_failed", action=action.action_type.value)
            return action

        action.status = ActionStatus.EXECUTED
        action.executed_at = datetime.now(UTC)
        action.result = result
        await self.audit.record(
            AuditAction.AGENT_ACTION_EXECUTED,
            user_id=action.approved_by_user_id,
            x_account_id=self.account.id,
            target_type="agent_action",
            target_id=str(action.id),
            note=action.summary,
            action_type=action.action_type.value,
            tier=action.autonomy_tier.value,
        )
        await self.db.flush()
        return action

    async def _gate(self, action: AgentAction) -> str | None:
        """Return a blocking reason, or None to proceed."""
        try:
            policy = policy_for(action.action_type)
        except PolicyViolationError as exc:
            return str(exc)

        if policy.tier is AutonomyTier.T3:
            return (
                "This class of action is not implemented by this system and never "
                "executes, regardless of approval."
            )

        if policy.tier is not AutonomyTier.T0 and action.status is not ActionStatus.APPROVED:
            return (
                f"{action.action_type.value} is tier {policy.tier.value} and needs the "
                f"account owner's approval before it can run."
            )

        if action.expires_at is not None and _aware(action.expires_at) <= datetime.now(UTC):
            return (
                "expired: The approval window lapsed before this ran. The analysis behind "
                "it is no longer current."
            )

        if policy.requires_write_flag and not self.settings.x_enable_write_actions:
            return (
                "Write actions are disabled (X_ENABLE_WRITE_ACTIONS is false). The "
                "`tweet.write` scope was never requested, so the stored token cannot "
                "authorise this even if the setting were changed without reconnecting."
            )

        if policy.requires_capability is not None:
            row = await self.db.scalar(
                select(AccountCapability).where(
                    AccountCapability.x_account_id == self.account.id,
                    AccountCapability.capability == policy.requires_capability,
                )
            )
            if row is None or row.status is not CapabilityStatus.AVAILABLE:
                observed = row.status.value if row is not None else "never probed"
                return (
                    f"The X API capability {policy.requires_capability.value} is "
                    f"{observed} for this account."
                )

        return None

    async def _perform(self, action: AgentAction, payload: dict[str, str]) -> dict[str, Any]:
        match action.action_type:
            case ActionType.RECORD_NOTE:
                await self.audit.system(
                    "INFO", "agent.note", payload["message"], x_account_id=self.account.id
                )
                return {"recorded": True}

            case ActionType.RAISE_ALERT:
                await self.audit.system(
                    "WARNING", "agent.alert", payload["message"], x_account_id=self.account.id
                )
                return {"raised": True}

            case ActionType.DRAFT_POST:
                # A draft is stored and nothing else. It reaches X only if the
                # owner separately promotes it to a PUBLISH_POST action, which
                # is a distinct T2 approval.
                return {"draft_text": payload["text"], "published": False}

            case ActionType.PROPOSE_TOPIC:
                existing = await self.db.scalar(
                    select(Topic).where(
                        Topic.x_account_id == self.account.id, Topic.name == payload["name"]
                    )
                )
                if existing is not None:
                    return {"topic_id": str(existing.id), "created": False}
                topic = Topic(
                    x_account_id=self.account.id,
                    name=payload["name"],
                    description=payload.get("description") or None,
                )
                self.db.add(topic)
                await self.db.flush()
                return {"topic_id": str(topic.id), "created": True}

            case ActionType.PUBLISH_POST:
                return await self._publish(payload["text"])

        raise PolicyViolationError(  # pragma: no cover — every member is handled above
            f"No handler for {action.action_type.value}."
        )

    async def _publish(self, text: str) -> dict[str, Any]:
        """The one outward-facing effect in the system.

        Reached only after every gate above has passed, which requires a human
        approval row, the write flag, the write scope and a probed capability.
        """
        from app.collectors.collector import build_collector

        collector = await build_collector(self.db, self.account)
        async with collector.client as client:
            response = await client.request(
                "tweets.create", json_body={"text": text}, expected_resources=1
            )
        created = response.data if isinstance(response.data, dict) else {}
        log.info("agent.post_published", x_post_id=created.get("id"))
        return {"published": True, "x_post_id": created.get("id"), "text": text}

    # -------------------------------------------------------------- upkeep
    async def expire_stale(self) -> int:
        """Lapse approval requests nobody answered.

        Pending actions are not harmless while they wait: approving one a week
        later would act on analysis that has since been superseded.
        """
        result = await self.db.execute(
            update(AgentAction)
            .where(
                AgentAction.x_account_id == self.account.id,
                AgentAction.status == ActionStatus.PENDING_APPROVAL,
                AgentAction.expires_at.isnot(None),
                AgentAction.expires_at <= datetime.now(UTC),
            )
            .values(
                status=ActionStatus.EXPIRED,
                blocked_reason="No decision was made within the approval window.",
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; Postgres does not."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)
