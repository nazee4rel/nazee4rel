"""Thin wrapper around the Anthropic Messages API.

Everything the rest of the agent needs from Claude goes through `ask()`, which
enforces four things the caller should not have to remember:

* **Structured output.** Responses are constrained to a Pydantic model via
  `messages.parse`, so nothing downstream parses prose.
* **Refusals are checked before content is read.** `stop_reason == "refusal"`
  means there is no usable output; treating it as an empty result would silently
  produce a run with no findings and no explanation.
* **The model holds no tools.** Not "is instructed not to use tools" — the
  request carries no `tools` parameter at all, so no output of the reasoning
  step can invoke anything. Actions are proposed as data and executed by
  `app.agent.executor` against a closed allowlist.
* **Caching where it pays.** The system prompt is long, stable, and identical
  across runs, so it carries a cache breakpoint. Per-run evidence goes in the
  user turn, after the breakpoint, which is what keeps the prefix reusable.

The whole surface is behind a Protocol so tests can inject a fake without
network access or an API key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

# Reasoning over an evidence bundle is not a long-output task; the structured
# schemas cap it further. Kept below the streaming threshold on purpose so the
# non-streaming path stays valid.
DEFAULT_MAX_TOKENS = 8000


class AgentModelError(Exception):
    """Base for every failure of the reasoning step."""


class ModelUnavailableError(AgentModelError):
    """No API key, or the call could not be made.

    Distinct from a refusal: the run degrades to PARTIAL and keeps its
    deterministic analytics rather than failing outright.
    """


class ModelRefusalError(AgentModelError):
    """The model declined to answer. There is no content to read."""


@dataclass(frozen=True)
class ModelResult[T]:
    parsed: T
    model: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int


class ReasoningClient(Protocol):
    """What the agent needs from a language model. Implemented by the real
    client and by the test double."""

    async def ask[T: BaseModel](
        self,
        *,
        system: str,
        user_content: str,
        output_format: type[T],
        model: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        effort: str | None = None,
    ) -> ModelResult[T]: ...


class AnthropicClient:
    """Production `ReasoningClient`."""

    def __init__(self, api_key: str | None = None, default_model: str | None = None) -> None:
        settings = get_settings()
        self._api_key = api_key if api_key is not None else settings.anthropic_api_key
        self._default_model = default_model or settings.anthropic_model_deep
        self._client: Any | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key)

    def _ensure_client(self) -> Any:
        if not self._api_key:
            raise ModelUnavailableError(
                "ANTHROPIC_API_KEY is not set. The agent's deterministic analytics still "
                "run; its reasoning, topic classification and recommendations do not."
            )
        if self._client is None:
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:  # pragma: no cover — declared dependency
                raise ModelUnavailableError(
                    "The `anthropic` package is not installed. Run `pip install -e '.[dev]'`."
                ) from exc
            self._client = AsyncAnthropic(api_key=self._api_key)
        return self._client

    async def ask[T: BaseModel](
        self,
        *,
        system: str,
        user_content: str,
        output_format: type[T],
        model: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        effort: str | None = None,
    ) -> ModelResult[T]:
        client = self._ensure_client()
        chosen = model or self._default_model

        # The system prompt is the cache prefix: identical on every run, and
        # long enough to clear the minimum block size. Evidence must stay in
        # the user turn or every run would invalidate the cache.
        system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        output_config: dict[str, Any] = {}
        if effort:
            output_config["effort"] = effort

        try:
            response = await client.messages.parse(
                model=chosen,
                max_tokens=max_tokens,
                system=system_blocks,
                messages=[{"role": "user", "content": user_content}],
                output_format=output_format,
                **({"output_config": output_config} if output_config else {}),
            )
        except AgentModelError:
            raise
        except Exception as exc:  # noqa: BLE001 — SDK raises a wide family
            raise ModelUnavailableError(f"The reasoning call failed: {exc}") from exc

        # Checked before content is touched: on a refusal there is no parsed
        # output, and reading it would look like a run that simply found
        # nothing.
        if getattr(response, "stop_reason", None) == "refusal":
            raise ModelRefusalError(
                "The model declined to respond to this request. Nothing has been stored."
            )

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise AgentModelError(
                f"The response did not contain structured output "
                f"(stop_reason={getattr(response, 'stop_reason', None)!r}). This usually "
                f"means the output was truncated; raise max_tokens."
            )
        if not isinstance(parsed, output_format):
            # Defensive: the SDK validates, but a schema-shaped dict slipping
            # through would reach the executor as untyped data.
            try:
                parsed = output_format.model_validate(parsed)
            except ValidationError as exc:
                raise AgentModelError(f"Structured output failed validation: {exc}") from exc

        usage = getattr(response, "usage", None)
        result = ModelResult(
            parsed=parsed,
            model=getattr(response, "model", chosen),
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cached_input_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        )
        log.info(
            "agent.model_call",
            model=result.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cached=result.cached_input_tokens,
            schema=output_format.__name__,
        )
        return result
