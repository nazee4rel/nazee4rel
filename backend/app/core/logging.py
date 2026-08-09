"""Structured logging.

JSON in production so logs are queryable; human-readable in development.

`scrub_secrets` is the important part: this application handles OAuth tokens and
passwords, and the most common way those leak is an exception rendered with its
arguments into a log line. The processor redacts by key name before anything is
emitted.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

_SENSITIVE_KEYS = {
    "password",
    "password_hash",
    "secret",
    "secret_key",
    "token",
    "access_token",
    "refresh_token",
    "access_token_enc",
    "refresh_token_enc",
    "authorization",
    "cookie",
    "set-cookie",
    "api_key",
    "anthropic_api_key",
    "x_client_secret",
    "code_verifier",
    "session_token",
    "totp_secret",
    "smtp_password",
    "postgres_password",
    "token_encryption_key",
    "client_secret",
}

_REDACTED = "***redacted***"
# Bound on recursion, so a cyclic structure cannot hang a log call.
_MAX_DEPTH = 6


def _scrub_value(value: Any, depth: int) -> Any:
    """Redact secret-shaped keys at any depth.

    Recursion matters here. Structured context is routinely nested — audit
    entries carry a `context` dict, collection results carry per-account
    dictionaries, and an exception logged with its arguments can nest several
    levels. A top-level-only scrubber redacts `refresh_token=...` and then
    prints the same value one level down inside `context`.

    Depth is bounded so a cyclic or pathologically nested structure cannot turn
    a log line into a hang.
    """
    if depth > _MAX_DEPTH:
        return value
    if isinstance(value, dict):
        return {
            key: _REDACTED
            if isinstance(key, str) and key.lower() in _SENSITIVE_KEYS
            else _scrub_value(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        cleaned = [_scrub_value(item, depth + 1) for item in value]
        return type(value)(cleaned) if isinstance(value, tuple) else cleaned
    return value


def scrub_secrets(
    _logger: Any, _method: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    for key in list(event_dict.keys()):
        if key.lower() in _SENSITIVE_KEYS:
            event_dict[key] = _REDACTED
        else:
            event_dict[key] = _scrub_value(event_dict[key], 0)
    return event_dict


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())

    # Uvicorn's access log duplicates our request middleware; silence it.
    logging.getLogger("uvicorn.access").disabled = True

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            scrub_secrets,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
