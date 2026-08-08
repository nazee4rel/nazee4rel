"""Application error types and their HTTP mapping.

Every error carries a stable machine-readable `code` so the frontend can branch
on behaviour rather than on prose. Internal detail never reaches the client:
unexpected exceptions are logged in full and returned as a generic 500 with a
correlation id the user can quote.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request, status
from fastapi.responses import JSONResponse

from app.core.logging import get_logger

log = get_logger(__name__)


class AppError(Exception):
    """Base class for expected, client-reportable failures."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "app_error"
    message: str = "Something went wrong."

    def __init__(self, message: str | None = None, **context: Any) -> None:
        self.message = message or self.message
        self.context = context
        super().__init__(self.message)


class AuthenticationError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "authentication_failed"
    message = "Invalid credentials."


class SessionExpiredError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "session_expired"
    message = "Your session has expired. Please sign in again."


class PermissionDeniedError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "permission_denied"
    message = "You do not have permission to perform this action."


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "Resource not found."


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    message = "That resource already exists."


class ValidationError(AppError):
    # Literal 422 rather than the Starlette constant, whose name changed
    # between versions (UNPROCESSABLE_ENTITY -> UNPROCESSABLE_CONTENT).
    status_code = 422
    code = "validation_error"
    message = "The submitted data is invalid."


class RateLimitError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"
    message = "Too many requests. Please slow down."

    def __init__(self, message: str | None = None, retry_after: int = 60, **ctx: Any) -> None:
        self.retry_after = retry_after
        super().__init__(message, **ctx)


class CapabilityUnavailableError(AppError):
    """Raised when the X API cannot provide something we need.

    Deliberately distinct from NotFound: it means "this metric does not exist
    at your access level", which the UI must render as UNAVAILABLE rather than
    as zero or as an error.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "capability_unavailable"
    message = "This data is not available at your current X API access level."


class BudgetExceededError(AppError):
    """The cost governor blocked an action that would exceed the API budget."""

    status_code = status.HTTP_402_PAYMENT_REQUIRED
    code = "budget_exceeded"
    message = "This action would exceed the configured X API monthly budget."


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    headers: dict[str, str] = {}
    if isinstance(exc, RateLimitError):
        headers["Retry-After"] = str(exc.retry_after)

    log.info(
        "request.handled_error",
        code=exc.code,
        path=request.url.path,
        status=exc.status_code,
        **exc.context,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
        headers=headers,
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    log.exception(
        "request.unhandled_error",
        path=request.url.path,
        method=request.method,
        request_id=request_id,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": {
                "code": "internal_error",
                "message": "An unexpected error occurred.",
                "request_id": request_id,
            }
        },
    )
