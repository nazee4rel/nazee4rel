"""Evaluating rules, and deciding what actually reaches a human.

Detection is the easy half and lives in `rules.py`. This module is the half that
determines whether the alerting system is usable a month from now:

* **Dedupe.** A finding carries the identity of its event. The same viral post
  crossing the threshold on eight consecutive runs is one alert row, not eight.
* **Cooldown.** Even for genuinely distinct events, a rule stays quiet for a
  configured period after it fires. A week with five bad days should produce a
  couple of alerts, not thirty-five.
* **Resolution.** Stateful rules describe conditions that end. When the
  detector stops returning a finding, the open alert is resolved automatically,
  so "collection has stopped" disappears when collection resumes rather than
  sitting there until someone dismisses it.
* **Severity floor.** Below the configured minimum an alert is still recorded
  but not delivered. The record is cheap; the interruption is not.

Every alert is written before any channel is contacted, so a failing mail server
loses a notification and never an alert.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.alerts import rules
from app.alerts.channels import Message, build_channels
from app.alerts.rules import AlertContext, Finding
from app.collectors import schedule
from app.core.logging import get_logger
from app.models.alerting import (
    Alert,
    AlertRule,
    AlertRuleKey,
    AlertSeverity,
    AlertState,
    Delivery,
    DeliveryStatus,
)
from app.models.content import AccountMetricSnapshot, Post
from app.models.enums import CapabilityStatus
from app.models.x_account import AccountCapability, OAuthToken, XAccount
from app.services.analytics_service import AnalyticsService
from app.services.cost_service import CostService

log = get_logger(__name__)

SEVERITY_ORDER = {
    AlertSeverity.INFO: 0,
    AlertSeverity.WARNING: 1,
    AlertSeverity.CRITICAL: 2,
}


@dataclass
class EvaluationResult:
    findings: int = 0
    created: list[Alert] = field(default_factory=list)
    suppressed_duplicate: int = 0
    suppressed_cooldown: int = 0
    suppressed_disabled: int = 0
    resolved: int = 0
    delivered: int = 0

    @property
    def summary(self) -> str:
        return (
            f"{self.findings} finding(s): {len(self.created)} raised, "
            f"{self.suppressed_duplicate} duplicate, {self.suppressed_cooldown} in cooldown, "
            f"{self.resolved} resolved"
        )


class AlertService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------- rule rows
    async def rules_for(self, x_account_id: uuid.UUID) -> dict[AlertRuleKey, AlertRule]:
        """Load rule configuration, creating defaults for anything new.

        Lazily rather than by migration, so adding a rule to the registry does
        not require a schema change or leave existing accounts unwatched.
        """
        existing = {
            row.rule_key: row
            for row in await self.db.scalars(
                select(AlertRule).where(AlertRule.x_account_id == x_account_id)
            )
        }
        missing = [key for key in rules.REGISTRY if key not in existing]
        for key in missing:
            spec = rules.REGISTRY[key]
            row = AlertRule(
                x_account_id=x_account_id,
                rule_key=key,
                enabled=True,
                cooldown_hours=spec.default_cooldown_hours,
                min_severity=AlertSeverity.INFO,
                thresholds={},
                channels=["DASHBOARD", "EMAIL"],
            )
            self.db.add(row)
            existing[key] = row
        if missing:
            await self.db.flush()
        return existing

    # ------------------------------------------------------------ evaluation
    async def build_context(self, account: XAccount, *, days: int = 30) -> AlertContext:
        """One read of everything the detectors need, so they all see the same instant."""
        now = datetime.now(UTC)
        analytics = AnalyticsService(self.db)

        summary = await analytics.dashboard_summary(account.id, days=days)
        series = await analytics.follower_series(account.id, days=days)
        performances = await analytics.post_performance(account.id, days=days)
        revenue = await analytics.revenue(account.id)
        budget = await CostService(self.db).budget_status(account.id)

        last_snapshot = await self.db.scalar(
            select(func.max(AccountMetricSnapshot.captured_at)).where(
                AccountMetricSnapshot.x_account_id == account.id
            )
        )
        hours_since: float | None = None
        if last_snapshot is not None:
            captured = last_snapshot if last_snapshot.tzinfo else last_snapshot.replace(tzinfo=UTC)
            hours_since = (now - captured).total_seconds() / 3600

        awaiting_freeze = int(
            await self.db.scalar(
                select(func.count())
                .select_from(Post)
                .where(
                    Post.x_account_id == account.id,
                    Post.final_freeze_at.is_(None),
                    Post.metrics_window_closes_at > now,
                    Post.metrics_window_closes_at
                    <= now
                    + timedelta(days=schedule.METRICS_WINDOW_DAYS - schedule.FREEZE_OPENS_AT_DAY),
                )
            )
            or 0
        )

        blocked = [
            row.capability.value
            for row in await self.db.scalars(
                select(AccountCapability).where(
                    AccountCapability.x_account_id == account.id,
                    AccountCapability.status.in_(
                        [CapabilityStatus.UNAVAILABLE, CapabilityStatus.FORBIDDEN]
                    ),
                )
            )
        ]
        refresh_failures = int(
            await self.db.scalar(
                select(func.coalesce(func.max(OAuthToken.refresh_failure_count), 0)).where(
                    OAuthToken.x_account_id == account.id
                )
            )
            or 0
        )

        revenue_summary = revenue["summary"]
        latest_month = revenue_summary.by_month[-1].month if revenue_summary.by_month else None

        return AlertContext(
            username=account.username,
            now=now,
            summary=summary,
            series=series,
            performances=performances,
            hours_since_snapshot=hours_since,
            posts_awaiting_freeze=awaiting_freeze,
            budget_state=str(budget["state"]),
            budget_fraction=float(str(budget["fraction_used"])),
            unavailable_capabilities=blocked,
            refresh_failure_count=refresh_failures,
            revenue_growth_ratio=revenue_summary.growth_ratio,
            revenue_currency=revenue_summary.currency,
            revenue_latest_month=latest_month,
        )

    async def evaluate(self, account: XAccount, *, days: int = 30) -> EvaluationResult:
        result = EvaluationResult()
        configured = await self.rules_for(account.id)
        enabled = {key for key, row in configured.items() if row.enabled}
        result.suppressed_disabled = len(configured) - len(enabled)

        context = await self.build_context(account, days=days)
        findings = rules.evaluate_all(context, enabled=enabled)
        result.findings = len(findings)
        fired_keys = {finding.key for finding in findings}

        for finding in findings:
            alert = await self._raise(account, finding, configured[finding.key], result)
            if alert is not None:
                result.created.append(alert)

        result.resolved = await self._resolve_cleared(
            account, fired_keys & set(rules.STATEFUL_RULES)
        )

        for alert in result.created:
            rule = configured[alert.rule_key]
            if SEVERITY_ORDER[alert.severity] < SEVERITY_ORDER[rule.min_severity]:
                # Recorded but not delivered. The row is cheap; the
                # interruption is not.
                continue
            if await self._deliver(alert, rule):
                result.delivered += 1

        await self.db.flush()
        log.info("alerts.evaluated", account=account.username, summary=result.summary)
        return result

    async def _raise(
        self,
        account: XAccount,
        finding: Finding,
        rule: AlertRule,
        result: EvaluationResult,
    ) -> Alert | None:
        existing = await self.db.scalar(
            select(Alert).where(
                Alert.x_account_id == account.id, Alert.dedupe_key == finding.dedupe_key
            )
        )
        if existing is not None:
            # Same event. A stateful condition that is still true should not be
            # re-raised, but it should stop looking resolved.
            if existing.state is AlertState.RESOLVED:
                existing.state = AlertState.FIRING
                existing.resolved_at = None
                # `fired_at` deliberately keeps its original value: it records
                # when the condition began, not when it was last observed.
            result.suppressed_duplicate += 1
            return None

        cutoff = datetime.now(UTC) - timedelta(hours=rule.cooldown_hours)
        recent = await self.db.scalar(
            select(func.count())
            .select_from(Alert)
            .where(
                Alert.x_account_id == account.id,
                Alert.rule_key == finding.key,
                Alert.fired_at >= cutoff,
            )
        )
        if recent:
            result.suppressed_cooldown += 1
            return None

        alert = Alert(
            x_account_id=account.id,
            rule_key=finding.key,
            severity=finding.severity,
            state=AlertState.FIRING,
            title=finding.title,
            body=finding.body,
            facts=dict(finding.facts),
            provenance=finding.provenance,
            dedupe_key=finding.dedupe_key,
            fired_at=datetime.now(UTC),
        )
        self.db.add(alert)
        await self.db.flush()
        return alert

    async def _resolve_cleared(self, account: XAccount, still_firing: set[AlertRuleKey]) -> int:
        """Close stateful alerts whose condition has gone away.

        Only stateful rules are eligible. "You gained 400 followers on Tuesday"
        is not a condition that can stop being true.
        """
        open_alerts = list(
            await self.db.scalars(
                select(Alert).where(
                    Alert.x_account_id == account.id,
                    Alert.state == AlertState.FIRING,
                    Alert.rule_key.in_(list(rules.STATEFUL_RULES)),
                )
            )
        )
        resolved = 0
        for alert in open_alerts:
            if alert.rule_key in still_firing:
                continue
            alert.state = AlertState.RESOLVED
            alert.resolved_at = datetime.now(UTC)
            resolved += 1
        if resolved:
            await self.db.flush()
        return resolved

    # ------------------------------------------------------------- delivery
    async def _deliver(self, alert: Alert, rule: AlertRule) -> bool:
        message = Message(
            subject=f"[{alert.severity.value}] {alert.title}",
            text=(
                f"{alert.title}\n\n{alert.body}\n\n"
                f"Rule: {alert.rule_key.value}\n"
                f"Provenance: {alert.provenance.value}\n"
                f"Raised: {alert.fired_at:%Y-%m-%d %H:%M} UTC\n"
            ),
        )
        any_sent = False
        for channel in build_channels(list(rule.channels or [])):
            outcome = await channel.send(message)
            self.db.add(
                Delivery(
                    alert_id=alert.id,
                    channel=channel.channel,
                    status=outcome.status,
                    target=outcome.target,
                    attempts=1,
                    sent_at=datetime.now(UTC) if outcome.status is DeliveryStatus.SENT else None,
                    error=outcome.error,
                )
            )
            any_sent = any_sent or outcome.status is DeliveryStatus.SENT
        await self.db.flush()
        return any_sent

    # ----------------------------------------------------------- user actions
    async def acknowledge(self, alert: Alert, user_id: uuid.UUID) -> Alert:
        """Mark an alert as seen.

        Acknowledgement is not resolution: a stateful alert whose condition is
        still true stays a live condition, and the detector — not a click — is
        what closes it.
        """
        if alert.state is AlertState.FIRING:
            alert.state = AlertState.ACKNOWLEDGED
            alert.acknowledged_at = datetime.now(UTC)
            alert.acknowledged_by_user_id = user_id
            await self.db.flush()
        return alert

    async def open_alerts(self, x_account_id: uuid.UUID, limit: int = 20) -> list[Alert]:
        return list(
            await self.db.scalars(
                select(Alert)
                .where(Alert.x_account_id == x_account_id, Alert.state == AlertState.FIRING)
                .order_by(Alert.fired_at.desc())
                .limit(limit)
            )
        )
