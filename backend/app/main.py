"""FastAPI application entrypoint."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import __version__
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.errors import AppError, app_error_handler, unhandled_error_handler
from app.core.logging import configure_logging, get_logger
from app.core.ratelimit import close_redis
from app.db.session import dispose_engine

settings = get_settings()
configure_logging(level=settings.log_level, json_output=settings.is_production)
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    log.info(
        "app.starting",
        environment=settings.environment,
        phase=2,
        write_actions_enabled=settings.x_enable_write_actions,
    )
    yield
    await close_redis()
    await dispose_engine()
    log.info("app.stopped")


app = FastAPI(
    title="X Account Intelligence Agent",
    description="Agentic monitoring and analysis for a single X/Twitter account.",
    version=__version__,
    lifespan=lifespan,
    # Interactive docs are useful locally and an unnecessary disclosure in prod.
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None,
    openapi_url=None if settings.is_production else "/openapi.json",
)

# Host header validation. `allowed_host_list` holds host *names* — an origin
# would never match, because a Host header carries no scheme, and the mistake is
# invisible until production rejects every request. The production validator in
# Settings refuses to boot without real hostnames here.
if settings.is_production:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_host_list)

# The browser never calls this API directly — Next.js proxies server-side — but
# CORS stays locked to the known origin so a stray direct call from a hostile
# page cannot ride the session cookie.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


@app.middleware("http")
async def request_context(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Attach a correlation id, bind logging context, add security headers."""
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    request.state.request_id = request_id

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        request_id=request_id, path=request.url.path, method=request.method
    )

    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if settings.is_production:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(Exception, unhandled_error_handler)

app.include_router(api_router)


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {"service": "x-account-intelligence-agent", "version": __version__}
