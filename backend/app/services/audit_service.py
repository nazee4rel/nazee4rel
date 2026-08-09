"""Writes to the append-only audit trail."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.audit import AuditLog, SystemLog
from app.models.enums import AuditAction

log = get_logger(__name__)

# Belt-and-braces: the log scrubber protects log output, but audit context is a
# database write, so it is filtered here too.
_FORBIDDEN_CONTEXT_KEYS = {
    "password",
    "new_password",
    "current_password",
    "token",
    "access_token",
    "refresh_token",
    "session_token",
    "code_verifier",
    "secret",
    "api_key",
}


def _clean(context: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in context.items() if k.lower() not in _FORBIDDEN_CONTEXT_KEYS}


def client_ip(request: Request | None) -> str | None:
    """Best-effort client IP.

    Trusts X-Forwarded-For only because this app is expected to sit behind a
    reverse proxy. If you expose it directly, strip that header at the edge or
    this value can be spoofed.
    """
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return request.client.host[:45] if request.client else None


def user_agent(request: Request | None) -> str | None:
    if request is None:
        return None
    ua = request.headers.get("user-agent")
    return ua[:512] if ua else None


class AuditService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def record(
        self,
        action: AuditAction,
        *,
        user_id: uuid.UUID | None = None,
        x_account_id: uuid.UUID | None = None,
        request: Request | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        note: str | None = None,
        **context: Any,
    ) -> AuditLog:
        entry = AuditLog(
            user_id=user_id,
            x_account_id=x_account_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            ip_address=client_ip(request),
            user_agent=user_agent(request),
            context=_clean(context),
            note=note,
        )
        self.db.add(entry)
        await self.db.flush()
        log.info("audit", action=action.value, user_id=str(user_id) if user_id else None)
        return entry

    async def system(
        self,
        level: str,
        component: str,
        message: str,
        *,
        x_account_id: uuid.UUID | None = None,
        **context: Any,
    ) -> SystemLog:
        entry = SystemLog(
            x_account_id=x_account_id,
            level=level.upper(),
            component=component,
            message=message,
            context=_clean(context),
        )
        self.db.add(entry)
        await self.db.flush()
        return entry
