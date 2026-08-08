"""X connection endpoints: OAuth handshake, probe, disconnect, usage."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, rate_limit
from app.core.config import get_settings
from app.core.errors import AppError, NotFoundError
from app.core.logging import get_logger
from app.integrations.x.errors import XApiError, XOAuthStateError
from app.integrations.x.probe import probe_account
from app.models.x_account import XAccount
from app.schemas.auth import MessageOut
from app.services.cost_service import CostService
from app.services.x_account_service import XAccountService

log = get_logger(__name__)
router = APIRouter(prefix="/x", tags=["x"])


class AuthorizeUrlOut(BaseModel):
    authorize_url: str
    requested_scopes: list[str]
    write_actions_enabled: bool


class ProbeResultOut(BaseModel):
    capability: str
    status: str
    detail: str


async def _get_account(db: DbSession, user: CurrentUser, account_id: uuid.UUID) -> XAccount:
    account = await db.scalar(
        select(XAccount).where(XAccount.id == account_id, XAccount.user_id == user.id)
    )
    if account is None:
        raise NotFoundError("X account not found.")
    return account


@router.post(
    "/oauth/start",
    response_model=AuthorizeUrlOut,
    dependencies=[Depends(rate_limit(limit=10, window_seconds=300, scope="x_oauth"))],
)
async def start_oauth(user: CurrentUser, db: DbSession) -> Any:
    """Begin the OAuth 2.0 + PKCE handshake.

    Returns the URL rather than redirecting, so the frontend controls navigation
    and the user can see where they are being sent before they go.
    """
    settings = get_settings()
    service = XAccountService(db)
    try:
        url = await service.begin_authorization(user.id)
    except XOAuthStateError as exc:
        raise AppError(str(exc)) from exc

    from app.integrations.x.endpoints import requested_scopes

    return AuthorizeUrlOut(
        authorize_url=url,
        requested_scopes=list(requested_scopes(settings.x_enable_write_actions)),
        write_actions_enabled=settings.x_enable_write_actions,
    )


@router.get("/oauth/callback", include_in_schema=False)
async def oauth_callback(
    request: Request,
    db: DbSession,
    state: str = Query(default=""),
    code: str = Query(default=""),
    error: str = Query(default=""),
) -> RedirectResponse:
    """Handle X's redirect back.

    Unauthenticated by design — the user arrives from x.com, so there is no
    session cookie guarantee. The `state` parameter is what authenticates the
    callback, which is precisely why it is single-use and expiring.

    Always redirects to the dashboard with a status in the query string; raising
    here would leave the user staring at raw JSON.
    """
    settings = get_settings()
    base = settings.frontend_origin.rstrip("/")

    if error:
        # The user declined, or X refused. Not an application failure.
        log.info("x.oauth.declined", error=error)
        return RedirectResponse(f"{base}/overview?x_connect=declined", status_code=303)

    if not state or not code:
        return RedirectResponse(f"{base}/overview?x_connect=invalid", status_code=303)

    service = XAccountService(db)
    try:
        account = await service.complete_authorization(state, code, request)
        await db.commit()
    except (XOAuthStateError, XApiError) as exc:
        log.warning("x.oauth.callback_failed", error=str(exc))
        return RedirectResponse(f"{base}/overview?x_connect=failed", status_code=303)

    # Probe immediately: the dashboard should never guess what is available, and
    # this is the first moment it can find out.
    try:
        await probe_account(db, account)
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        # A failed probe leaves capabilities UNKNOWN, which the UI renders
        # honestly. It must not undo a successful connection.
        log.warning("x.probe.failed_after_connect", error=str(exc))

    return RedirectResponse(f"{base}/overview?x_connect=success", status_code=303)


@router.post("/accounts/{account_id}/probe", response_model=list[ProbeResultOut])
async def run_probe(account_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Any:
    """Re-run the capability probe.

    Costs a handful of resources. Worth running after a billing change, or when
    a previously inconclusive capability may now be decidable.
    """
    account = await _get_account(db, user, account_id)
    try:
        results = await probe_account(db, account)
    except XApiError as exc:
        raise AppError(str(exc)) from exc

    return [
        ProbeResultOut(capability=r.capability.value, status=r.status.value, detail=r.detail)
        for r in results
    ]


@router.delete("/accounts/{account_id}", response_model=MessageOut)
async def disconnect_account(
    account_id: uuid.UUID, user: CurrentUser, db: DbSession, request: Request
) -> MessageOut:
    """Disconnect an X account.

    Revokes and deletes credentials but keeps collected history: impressions
    older than 30 days cannot be re-fetched from X at any price, so discarding
    them would be irreversible in a way disconnecting is not.
    """
    account = await _get_account(db, user, account_id)
    await XAccountService(db).disconnect(account, request)
    return MessageOut(
        message=(
            f"Disconnected @{account.username}. Collected history has been kept — "
            "reconnecting resumes where it left off."
        )
    )


@router.get("/usage")
async def api_usage(user: CurrentUser, db: DbSession) -> Any:
    """Month-to-date X API spend, by endpoint.

    Under pay-per-use every read costs money, so this is surfaced rather than
    left to a monthly invoice.
    """
    accounts = list(await db.scalars(select(XAccount.id).where(XAccount.user_id == user.id)))
    account_id = accounts[0] if len(accounts) == 1 else None
    return await CostService(db).usage_summary(account_id)
