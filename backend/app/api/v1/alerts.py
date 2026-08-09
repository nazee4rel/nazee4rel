"""Alerts, rules and reports.

Two endpoints here are deliberately shaped against habit. `acknowledge` marks
an alert as seen but does not resolve it: a stateful condition is closed by the
detector observing that it has cleared, never by a click, so "collection has
stopped" cannot be dismissed while collection is still stopped. And the rule
endpoints expose `cooldown_hours` prominently, because tuning that is what keeps
this system worth listening to.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.alerts import rules as rule_registry
from app.alerts.service import AlertService
from app.api.deps import CurrentUser, DbSession, rate_limit
from app.core.errors import NotFoundError, ValidationError
from app.models.alerting import (
    Alert,
    AlertRuleKey,
    AlertSeverity,
    AlertState,
    Delivery,
    Report,
    ReportPeriod,
)
from app.models.x_account import XAccount
from app.reports.service import ReportService, render_text

router = APIRouter(prefix="/alerts", tags=["alerts"])


# ------------------------------------------------------------------- schemas
class AlertOut(BaseModel):
    id: uuid.UUID
    rule_key: str
    severity: str
    state: str
    title: str
    body: str
    facts: dict[str, Any]
    provenance: str
    fired_at: datetime
    acknowledged_at: datetime | None
    resolved_at: datetime | None
    # Stateful alerts close themselves; the UI should not offer to close them.
    is_stateful: bool


class RuleOut(BaseModel):
    rule_key: str
    title: str
    description: str
    enabled: bool
    cooldown_hours: int
    min_severity: str
    channels: list[str]
    is_stateful: bool


class RuleUpdate(BaseModel):
    enabled: bool | None = None
    cooldown_hours: int | None = Field(default=None, ge=0, le=720)
    min_severity: AlertSeverity | None = None
    channels: list[str] | None = None


class DeliveryOut(BaseModel):
    id: uuid.UUID
    channel: str
    status: str
    target: str | None
    error: str | None
    sent_at: datetime | None
    created_at: datetime


class ReportOut(BaseModel):
    id: uuid.UUID
    period: str
    period_start: date
    period_end: date
    generated_at: datetime
    title: str
    summary: str
    sections: list[dict[str, Any]]
    status: str


class EvaluationOut(BaseModel):
    findings: int
    raised: int
    suppressed_duplicate: int
    suppressed_cooldown: int
    resolved: int
    delivered: int
    summary: str


# ------------------------------------------------------------------- helpers
async def _account(db: DbSession, user: CurrentUser, account_id: uuid.UUID) -> XAccount:
    account = await db.scalar(
        select(XAccount).where(XAccount.id == account_id, XAccount.user_id == user.id)
    )
    if account is None:
        raise NotFoundError("X account not found.")
    return account


def _alert_out(alert: Alert) -> AlertOut:
    return AlertOut(
        id=alert.id,
        rule_key=alert.rule_key.value,
        severity=alert.severity.value,
        state=alert.state.value,
        title=alert.title,
        body=alert.body,
        facts=dict(alert.facts),
        provenance=alert.provenance.value,
        fired_at=alert.fired_at,
        acknowledged_at=alert.acknowledged_at,
        resolved_at=alert.resolved_at,
        is_stateful=alert.rule_key in rule_registry.STATEFUL_RULES,
    )


# -------------------------------------------------------------------- alerts
@router.get("/{account_id}", response_model=list[AlertOut])
async def list_alerts(
    account_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    open_only: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[AlertOut]:
    account = await _account(db, user, account_id)
    stmt = select(Alert).where(Alert.x_account_id == account.id)
    if open_only:
        stmt = stmt.where(Alert.state == AlertState.FIRING)
    rows = list(await db.scalars(stmt.order_by(Alert.fired_at.desc()).limit(limit)))
    return [_alert_out(row) for row in rows]


@router.post("/{alert_id}/acknowledge", response_model=AlertOut)
async def acknowledge_alert(alert_id: uuid.UUID, user: CurrentUser, db: DbSession) -> AlertOut:
    """Mark an alert as seen.

    Not the same as resolving it. A stateful alert whose condition is still
    true remains a live condition — acknowledging it stops it demanding
    attention without pretending the problem went away.
    """
    alert = await db.scalar(
        select(Alert)
        .join(XAccount, XAccount.id == Alert.x_account_id)
        .where(Alert.id == alert_id, XAccount.user_id == user.id)
    )
    if alert is None:
        raise NotFoundError("Alert not found.")
    return _alert_out(await AlertService(db).acknowledge(alert, user.id))


@router.post(
    "/{account_id}/evaluate",
    response_model=EvaluationOut,
    dependencies=[Depends(rate_limit(limit=12, window_seconds=3600, scope="alert-eval"))],
)
async def evaluate_now(account_id: uuid.UUID, user: CurrentUser, db: DbSession) -> EvaluationOut:
    """Run every enabled rule now, applying the same dedupe and cooldown."""
    account = await _account(db, user, account_id)
    result = await AlertService(db).evaluate(account)
    return EvaluationOut(
        findings=result.findings,
        raised=len(result.created),
        suppressed_duplicate=result.suppressed_duplicate,
        suppressed_cooldown=result.suppressed_cooldown,
        resolved=result.resolved,
        delivered=result.delivered,
        summary=result.summary,
    )


# --------------------------------------------------------------------- rules
@router.get("/{account_id}/rules", response_model=list[RuleOut])
async def list_rules(account_id: uuid.UUID, user: CurrentUser, db: DbSession) -> list[RuleOut]:
    account = await _account(db, user, account_id)
    configured = await AlertService(db).rules_for(account.id)
    return [
        RuleOut(
            rule_key=key.value,
            title=rule_registry.REGISTRY[key].title,
            description=rule_registry.REGISTRY[key].description,
            enabled=row.enabled,
            cooldown_hours=row.cooldown_hours,
            min_severity=row.min_severity.value,
            channels=list(row.channels or []),
            is_stateful=rule_registry.REGISTRY[key].is_stateful,
        )
        for key, row in sorted(configured.items(), key=lambda item: item[0].value)
    ]


@router.patch("/{account_id}/rules/{rule_key}", response_model=RuleOut)
async def update_rule(
    account_id: uuid.UUID,
    rule_key: AlertRuleKey,
    body: RuleUpdate,
    user: CurrentUser,
    db: DbSession,
) -> RuleOut:
    account = await _account(db, user, account_id)
    configured = await AlertService(db).rules_for(account.id)
    row = configured.get(rule_key)
    if row is None:  # pragma: no cover — rules_for creates every registry key
        raise NotFoundError("Rule not found.")

    if body.enabled is not None:
        row.enabled = body.enabled
    if body.cooldown_hours is not None:
        row.cooldown_hours = body.cooldown_hours
    if body.min_severity is not None:
        row.min_severity = body.min_severity
    if body.channels is not None:
        allowed = {"DASHBOARD", "EMAIL"}
        unknown = set(body.channels) - allowed
        if unknown:
            raise ValidationError(f"Unknown channel(s): {', '.join(sorted(unknown))}.")
        row.channels = body.channels
    await db.flush()

    spec = rule_registry.REGISTRY[rule_key]
    return RuleOut(
        rule_key=rule_key.value,
        title=spec.title,
        description=spec.description,
        enabled=row.enabled,
        cooldown_hours=row.cooldown_hours,
        min_severity=row.min_severity.value,
        channels=list(row.channels or []),
        is_stateful=spec.is_stateful,
    )


# ------------------------------------------------------------------ reports
@router.get("/{account_id}/reports", response_model=list[ReportOut])
async def list_reports(
    account_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    period: ReportPeriod | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> Any:
    account = await _account(db, user, account_id)
    stmt = select(Report).where(Report.x_account_id == account.id)
    if period is not None:
        stmt = stmt.where(Report.period == period)
    return list(await db.scalars(stmt.order_by(Report.generated_at.desc()).limit(limit)))


@router.get("/{account_id}/reports/{report_id}/text", response_model=dict)
async def report_as_text(
    account_id: uuid.UUID, report_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> dict[str, str]:
    """The plain-text rendering — the same body that would be emailed."""
    account = await _account(db, user, account_id)
    report = await db.scalar(
        select(Report).where(Report.id == report_id, Report.x_account_id == account.id)
    )
    if report is None:
        raise NotFoundError("Report not found.")
    return {"text": render_text(report)}


@router.post(
    "/{account_id}/reports",
    response_model=ReportOut,
    dependencies=[Depends(rate_limit(limit=10, window_seconds=3600, scope="report-gen"))],
)
async def generate_report(
    account_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    period: ReportPeriod = ReportPeriod.WEEKLY,
    deliver: bool = False,
) -> Any:
    """Generate a report now.

    `deliver` defaults to false: generating one to look at should not send an
    email, and re-running a period amends the stored report rather than
    producing a second one.
    """
    account = await _account(db, user, account_id)
    return await ReportService(db).generate(account, period, deliver=deliver)


# --------------------------------------------------------------- deliveries
@router.get("/{account_id}/deliveries", response_model=list[DeliveryOut])
async def list_deliveries(
    account_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=50, ge=1, le=200),
) -> Any:
    """Why you did or did not hear about something, with timestamps."""
    account = await _account(db, user, account_id)
    return list(
        await db.scalars(
            select(Delivery)
            .outerjoin(Alert, Alert.id == Delivery.alert_id)
            .outerjoin(Report, Report.id == Delivery.report_id)
            .where(
                (Alert.x_account_id == account.id) | (Report.x_account_id == account.id),
            )
            .order_by(Delivery.created_at.desc())
            .limit(limit)
        )
    )


@router.get("/{account_id}/channels", response_model=dict)
async def channel_status(account_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Any:
    """Which channels are actually configured.

    Worth surfacing: an unconfigured email channel is not an error, but it does
    mean nothing reaches you when you are not looking at the dashboard.
    """
    from app.alerts.channels import DashboardChannel, EmailChannel

    await _account(db, user, account_id)
    email = EmailChannel()
    return {
        "channels": [
            {
                "channel": "DASHBOARD",
                "configured": DashboardChannel().is_configured,
                "note": "Always available. The alert row is the delivery.",
            },
            {
                "channel": "EMAIL",
                "configured": email.is_configured,
                "note": (
                    "Configured via SMTP_HOST, ALERT_EMAIL_FROM and ALERT_EMAIL_TO."
                    if email.is_configured
                    else "Not configured. Alerts are still recorded in the dashboard; "
                    "nothing is sent."
                ),
            },
        ]
    }
