"""The cost governor.

X bills reads per resource under pay-per-use, so a collector that polls
enthusiastically spends real money. This service is the single place that
decides whether a call is affordable, and the single place that records what it
cost.

Two deliberate choices:

* **Pre-authorise, then reconcile.** The estimate before a call uses the maximum
  resources the request could return; the ledger afterwards records what it
  actually returned. Estimating high means the budget can never be overshot by a
  call that turns out larger than expected.
* **Blocked calls are still recorded.** A row with `was_blocked=True` costs
  nothing but explains why collection has a hole. Silently skipping would leave
  a gap in the data with no evidence of why — and gaps in this dataset are
  permanent.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.integrations.x.endpoints import CostClass, Endpoint
from app.models.usage import ApiUsageLedger

log = get_logger(__name__)

BudgetState = Literal["healthy", "warning", "critical", "exhausted"]

# Fractions of the monthly budget at which the Phase 4 scheduler steps its
# collection cadence down before it is forced to stop entirely.
WARNING_THRESHOLD = 0.70
CRITICAL_THRESHOLD = 0.90


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class CostService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.settings = get_settings()

    # -------------------------------------------------------------- pricing
    @property
    def budget_micros(self) -> int:
        return int(round(self.settings.x_monthly_budget_usd * 1_000_000))

    def rate_for(self, cost_class: CostClass) -> int:
        """Micro-USD per resource for a cost class."""
        if cost_class is CostClass.FREE:
            return 0
        if cost_class is CostClass.OWNED_READ:
            return self.settings.x_cost_owned_read_micros
        return self.settings.x_cost_general_read_micros

    def estimate_micros(self, endpoint: Endpoint, expected_resources: int) -> int:
        # Legacy subscription tiers are flat-rate: reads are bounded by rate
        # limits rather than billed per resource, so there is nothing to govern.
        if self.settings.x_billing_mode == "legacy_subscription":
            return 0
        return self.rate_for(endpoint.cost_class) * max(expected_resources, 0)

    # --------------------------------------------------------------- budget
    async def spent_this_month(self, x_account_id: uuid.UUID | None = None) -> int:
        stmt = select(func.coalesce(func.sum(ApiUsageLedger.estimated_cost_micros), 0)).where(
            ApiUsageLedger.created_at >= month_start(),
            ApiUsageLedger.was_blocked.is_(False),
        )
        if x_account_id is not None:
            stmt = stmt.where(ApiUsageLedger.x_account_id == x_account_id)
        return int(await self.db.scalar(stmt) or 0)

    async def budget_status(self, x_account_id: uuid.UUID | None = None) -> dict[str, object]:
        spent = await self.spent_this_month(x_account_id)
        budget = self.budget_micros
        fraction = (spent / budget) if budget > 0 else 1.0

        state: BudgetState = "healthy"
        if fraction >= 1.0:
            state = "exhausted"
        elif fraction >= CRITICAL_THRESHOLD:
            state = "critical"
        elif fraction >= WARNING_THRESHOLD:
            state = "warning"

        return {
            "state": state,
            "spent_micros": spent,
            "budget_micros": budget,
            "spent_usd": round(spent / 1_000_000, 4),
            "budget_usd": round(budget / 1_000_000, 2),
            "fraction_used": round(fraction, 4),
            "billing_mode": self.settings.x_billing_mode,
            "period_start": month_start().isoformat(),
        }

    async def can_afford(
        self, endpoint: Endpoint, expected_resources: int, x_account_id: uuid.UUID | None = None
    ) -> tuple[bool, int, int]:
        """Return (affordable, estimated_micros, spent_micros)."""
        estimate = self.estimate_micros(endpoint, expected_resources)
        if estimate == 0:
            return True, 0, await self.spent_this_month(x_account_id)

        spent = await self.spent_this_month(x_account_id)
        return spent + estimate <= self.budget_micros, estimate, spent

    # --------------------------------------------------------------- ledger
    async def record(
        self,
        endpoint: Endpoint,
        *,
        x_account_id: uuid.UUID | None,
        resources_returned: int = 0,
        status_code: int | None = None,
        latency_ms: int | None = None,
        rate_limit_remaining: int | None = None,
        rate_limit_reset_at: datetime | None = None,
        was_blocked: bool = False,
        block_reason: str | None = None,
        error: str | None = None,
        **context: object,
    ) -> ApiUsageLedger:
        cost = 0 if was_blocked else self.estimate_micros(endpoint, resources_returned)

        entry = ApiUsageLedger(
            x_account_id=x_account_id,
            endpoint_key=endpoint.key,
            method=endpoint.method.value,
            cost_class=endpoint.cost_class.value,
            resources_returned=resources_returned,
            estimated_cost_micros=cost,
            billing_mode=self.settings.x_billing_mode,
            status_code=status_code,
            latency_ms=latency_ms,
            rate_limit_remaining=rate_limit_remaining,
            rate_limit_reset_at=rate_limit_reset_at,
            was_blocked=was_blocked,
            block_reason=block_reason,
            error=error,
            context=dict(context),
        )
        self.db.add(entry)
        await self.db.flush()
        return entry

    async def usage_summary(self, x_account_id: uuid.UUID | None = None) -> dict[str, object]:
        """Month-to-date spend, broken down by endpoint, for the dashboard."""
        stmt = (
            select(
                ApiUsageLedger.endpoint_key,
                func.count().label("calls"),
                func.coalesce(func.sum(ApiUsageLedger.resources_returned), 0).label("resources"),
                func.coalesce(func.sum(ApiUsageLedger.estimated_cost_micros), 0).label("cost"),
            )
            .where(ApiUsageLedger.created_at >= month_start())
            .group_by(ApiUsageLedger.endpoint_key)
            .order_by(func.sum(ApiUsageLedger.estimated_cost_micros).desc())
        )
        if x_account_id is not None:
            stmt = stmt.where(ApiUsageLedger.x_account_id == x_account_id)

        rows = (await self.db.execute(stmt)).all()
        blocked = await self.db.scalar(
            select(func.count())
            .select_from(ApiUsageLedger)
            .where(
                ApiUsageLedger.created_at >= month_start(),
                ApiUsageLedger.was_blocked.is_(True),
            )
        )

        return {
            "budget": await self.budget_status(x_account_id),
            "blocked_calls": int(blocked or 0),
            "by_endpoint": [
                {
                    "endpoint": row.endpoint_key,
                    "calls": int(row.calls),
                    "resources": int(row.resources),
                    "cost_usd": round(int(row.cost) / 1_000_000, 4),
                }
                for row in rows
            ],
        }
