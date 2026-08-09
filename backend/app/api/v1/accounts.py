"""Connected X account endpoints.

Phase 2 exposes read-only state so the dashboard has something real to render.
The OAuth connect flow and the live capability probe arrive in Phase 3; until
then `status` reports NOT_CONNECTED and the frontend shows the connect prompt.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, DbSession
from app.core.config import get_settings
from app.core.errors import NotFoundError
from app.models.enums import CapabilityStatus, XCapability
from app.models.x_account import AccountCapability, XAccount

router = APIRouter(prefix="/accounts", tags=["accounts"])


class CapabilityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    capability: XCapability
    status: CapabilityStatus
    last_checked_at: datetime | None
    last_error: str | None


class XAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    x_user_id: str
    username: str
    display_name: str | None
    profile_image_url: str | None
    is_active: bool
    connected_at: datetime | None
    # Nothing before this timestamp can ever be backfilled, because non-public
    # metrics vanish from the X API 30 days after a post is created.
    collection_started_at: datetime | None
    capabilities: list[CapabilityOut] = []


class AccountsStatusOut(BaseModel):
    connected: bool
    accounts: list[XAccountOut]
    # Surfaced so the UI can explain *why* posting is unavailable rather than
    # just hiding the button.
    write_actions_enabled: bool
    billing_mode: str
    monthly_budget_usd: float


@router.get("", response_model=AccountsStatusOut)
async def list_accounts(user: CurrentUser, db: DbSession) -> Any:
    accounts = list(
        await db.scalars(
            select(XAccount)
            .where(XAccount.user_id == user.id, XAccount.is_active.is_(True))
            .options(selectinload(XAccount.capabilities))
            .order_by(XAccount.created_at)
        )
    )
    settings = get_settings()
    return AccountsStatusOut(
        connected=len(accounts) > 0,
        accounts=[XAccountOut.model_validate(a) for a in accounts],
        write_actions_enabled=settings.x_enable_write_actions,
        billing_mode=settings.x_billing_mode,
        monthly_budget_usd=settings.x_monthly_budget_usd,
    )


@router.get("/{account_id}/capabilities", response_model=list[CapabilityOut])
async def get_capabilities(account_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Any:
    """The capability matrix for one account.

    Every capability the system knows about is returned, including ones never
    probed (status UNKNOWN). That is deliberate — "not yet checked" and
    "unavailable" are different facts and the UI must not conflate them.
    """
    account = await db.scalar(
        select(XAccount).where(XAccount.id == account_id, XAccount.user_id == user.id)
    )
    if account is None:
        raise NotFoundError("X account not found.")

    rows = list(
        await db.scalars(
            select(AccountCapability).where(AccountCapability.x_account_id == account_id)
        )
    )
    known = {r.capability: r for r in rows}

    return [
        CapabilityOut.model_validate(known[cap])
        if cap in known
        else CapabilityOut(
            capability=cap,
            status=CapabilityStatus.UNKNOWN,
            last_checked_at=None,
            last_error=None,
        )
        for cap in XCapability
    ]
