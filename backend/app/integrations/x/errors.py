"""Errors raised by the X integration.

Distinguishing these matters because the correct response differs sharply:
a 429 should be retried after a wait, a 403 should mark a capability
unavailable and stop retrying forever, and a failed refresh means the user must
reconnect and should be told so rather than watching collection silently stop.
"""

from __future__ import annotations

from typing import Any


class XApiError(Exception):
    """Base class for X API failures."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        endpoint: str | None = None,
        payload: Any = None,
    ) -> None:
        self.status_code = status_code
        self.endpoint = endpoint
        self.payload = payload
        super().__init__(message)


class XAuthError(XApiError):
    """401 — the access token is missing, expired or rejected."""


class XForbiddenError(XApiError):
    """403 — authenticated, but this access level or scope set may not do this.

    Treated as a capability signal rather than a transient failure: retrying
    will not help, and the right move is to mark the capability unavailable and
    tell the user what changed.
    """


class XRateLimitError(XApiError):
    """429 — retry after the window resets."""

    def __init__(self, message: str, *, retry_after: int = 60, **kwargs: Any) -> None:
        self.retry_after = retry_after
        super().__init__(message, **kwargs)


class XNotFoundError(XApiError):
    """404 — the resource does not exist, or is not visible to this token."""


class XServerError(XApiError):
    """5xx — X's problem. Retryable with backoff."""


class XTokenRefreshError(XApiError):
    """The refresh token could not be exchanged.

    X refresh tokens are single-use and rotate on every exchange, so a failure
    here can mean the token was already consumed. Recovery requires the user to
    reconnect their account; there is no way to repair it server-side.
    """


class XOAuthStateError(XApiError):
    """The OAuth callback's state parameter was missing, unknown, reused or expired.

    Any of those is either a CSRF attempt or a replayed callback, so the
    handshake is refused rather than repaired.
    """


class XBudgetExceededError(XApiError):
    """The cost governor refused a request that would breach the monthly budget."""

    def __init__(self, message: str, *, spent_micros: int, budget_micros: int) -> None:
        self.spent_micros = spent_micros
        self.budget_micros = budget_micros
        super().__init__(message)


class XCapabilityUnavailableError(XApiError):
    """A call was attempted for a capability the probe found unavailable."""
