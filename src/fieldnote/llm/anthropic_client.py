"""Anthropic Claude implementation of :class:`LLMClient` using the official ``anthropic`` SDK.

Structured output uses forced tool use: the schema becomes the input schema of a single tool and
``tool_choice`` forces the model to call it. If the model rejects forced tool choice (some newer
models only accept ``auto``), the client falls back to ``auto`` plus an explicit instruction and
checks that the tool was actually called. Validation failures are fed back as an error
``tool_result`` and retried up to two times.
"""

from __future__ import annotations

import time
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from fieldnote.config import Settings
from fieldnote.costs import CostTracker
from fieldnote.llm.base import (
    ChatResult,
    LLMClient,
    LLMError,
    LLMUnavailableError,
    ToolCall,
    ToolSpec,
    Usage,
    schema_tool,
)
from fieldnote.llm.cache import LLMCacheStore
from fieldnote.logging_setup import get_logger, redact

log = get_logger(__name__)
T = TypeVar("T", bound=BaseModel)

MAX_VALIDATION_RETRIES = 2


class AnthropicClient(LLMClient):
    name = "anthropic"
    is_mock = False

    def __init__(
        self,
        settings: Settings,
        *,
        cache: LLMCacheStore | None = None,
        cost: CostTracker | None = None,
    ) -> None:
        super().__init__(settings, cache=cache, cost=cost)
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=3, timeout=180.0)
        self._forced_tool_supported: dict[str, bool] = {}
        self._thinking_disabled_needed: dict[str, bool] = {}

    # ---- low-level call with typed error handling ------------------------------------------
    def _create(self, **kwargs: Any) -> Any:
        model = kwargs["model"]
        if self._thinking_disabled_needed.get(model) and kwargs.get("tool_choice", {}).get("type") in ("tool", "any"):
            kwargs["thinking"] = {"type": "disabled"}
        a = self._anthropic
        try:
            return self._client.messages.create(**kwargs)
        except a.BadRequestError as exc:
            msg = str(getattr(exc, "message", exc)).lower()
            tc = kwargs.get("tool_choice", {})
            if "tool_choice" in msg and tc.get("type") in ("tool", "any") and "thinking" not in msg:
                log.info("model %s rejects forced tool_choice; switching to auto + instruction", model)
                self._forced_tool_supported[model] = False
                raise _RetryWithAuto from exc
            if (
                "thinking" in msg
                and tc.get("type") in ("tool", "any")
                and not self._thinking_disabled_needed.get(model)
            ):
                log.info("model %s needs thinking disabled for forced tool use; retrying", model)
                self._thinking_disabled_needed[model] = True
                kwargs["thinking"] = {"type": "disabled"}
                return self._client.messages.create(**kwargs)
            raise LLMError(f"bad request: {redact(str(exc))}") from exc
        except a.AuthenticationError as exc:
            raise LLMUnavailableError("Anthropic authentication failed; check ANTHROPIC_API_KEY", 3600) from exc
        except a.PermissionDeniedError as exc:
            raise LLMUnavailableError("Anthropic API key lacks permission for this model", 3600) from exc
        except a.NotFoundError as exc:
            raise LLMUnavailableError(
                f"model '{model}' not found; check FIELDNOTE_MODEL / FIELDNOTE_FAST_MODEL", 3600
            ) from exc
        except a.RateLimitError as exc:
            raise LLMUnavailableError("Anthropic rate limit hit after retries", 600) from exc
        except a.APIStatusError as exc:
            if exc.status_code >= 500:
                raise LLMUnavailableError(f"Anthropic API error {exc.status_code}: {redact(str(exc))}", 600) from exc
            raise LLMError(f"Anthropic API error {exc.status_code}: {redact(str(exc))}") from exc
        except a.APIConnectionError as exc:
            raise LLMUnavailableError("network error talking to the Anthropic API", 600) from exc

    @staticmethod
    def _usage(resp: Any, model: str, latency_ms: int) -> Usage:
        u = getattr(resp, "usage", None)
        return Usage(
            model=model,
            input_tokens=int(getattr(u, "input_tokens", 0) or 0),
            output_tokens=int(getattr(u, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
            cache_write_tokens=int(getattr(u, "cache_creation_input_tokens", 0) or 0),
            latency_ms=latency_ms,
        )

    @staticmethod
    def _content_dicts(resp: Any) -> list[dict[str, Any]]:
        out = []
        for block in resp.content:
            if hasattr(block, "model_dump"):
                out.append(block.model_dump(exclude_none=True, mode="json"))
            else:  # pragma: no cover - defensive
                out.append(dict(block))
        return out

    def _system_blocks(self, system: str) -> list[dict[str, Any]]:
        # Cache the stable system prompt; volatile content lives in the user turn.
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    # ---- structured output ------------------------------------------------------------------
    def _structured_impl(
        self,
        *,
        task: str,
        model: str,
        system: str,
        prompt: str,
        schema: type[T],
        payload: dict[str, Any],
        max_tokens: int,
    ) -> tuple[T, list[Usage]]:
        tool = schema_tool(schema, task)
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        usages: list[Usage] = []
        last_error = "no tool call"
        for _attempt in range(1 + MAX_VALIDATION_RETRIES):
            forced = self._forced_tool_supported.get(model, True)
            sys_text = (
                system if forced else f"{system}\n\nAlways respond by calling the `{tool.name}` tool exactly once."
            )
            kwargs: dict[str, Any] = {
                "model": model,
                "max_tokens": max_tokens,
                "system": self._system_blocks(sys_text),
                "messages": messages,
                "tools": [tool.to_api()],
                "tool_choice": {"type": "tool", "name": tool.name} if forced else {"type": "auto"},
            }
            start = time.perf_counter()
            try:
                resp = self._create(**kwargs)
            except _RetryWithAuto:
                continue
            usages.append(self._usage(resp, model, int((time.perf_counter() - start) * 1000)))
            if resp.stop_reason == "refusal":
                raise LLMError(f"model declined the '{task}' request")
            tool_use = next((b for b in resp.content if b.type == "tool_use" and b.name == tool.name), None)
            if tool_use is None:
                last_error = "the model did not call the tool"
                messages = [
                    *messages,
                    {
                        "role": "assistant",
                        "content": self._content_dicts(resp) or [{"type": "text", "text": "(no output)"}],
                    },
                    {"role": "user", "content": f"Call the `{tool.name}` tool now with the complete result."},
                ]
                continue
            try:
                return schema.model_validate(tool_use.input), usages
            except ValidationError as exc:
                last_error = str(exc)
                log.info("structured output for %s failed validation; retrying with feedback", task)
                messages = [
                    *messages,
                    {"role": "assistant", "content": self._content_dicts(resp)},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use.id,
                                "is_error": True,
                                "content": f"Schema validation failed:\n{exc}\nCall `{tool.name}` again with corrected input.",
                            }
                        ],
                    },
                ]
        raise LLMError(f"structured output for '{task}' failed after retries: {last_error[:500]}")

    # ---- tool-use chat ----------------------------------------------------------------------
    def _chat_impl(
        self,
        *,
        task: str,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
        force_tool: str | None,
        payload: dict[str, Any],
        max_tokens: int,
    ) -> ChatResult:
        forced_ok = self._forced_tool_supported.get(model, True)
        sys_text = system
        if force_tool and not forced_ok:
            sys_text = f"{system}\n\nYou must now call the `{force_tool}` tool."
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": self._system_blocks(sys_text),
            "messages": messages,
            "tools": [t.to_api() for t in tools],
            "tool_choice": {"type": "tool", "name": force_tool} if (force_tool and forced_ok) else {"type": "auto"},
        }
        start = time.perf_counter()
        try:
            resp = self._create(**kwargs)
        except _RetryWithAuto:
            kwargs["tool_choice"] = {"type": "auto"}
            kwargs["system"] = self._system_blocks(f"{system}\n\nYou must now call the `{force_tool}` tool.")
            resp = self._create(**kwargs)
        usage = self._usage(resp, model, int((time.perf_counter() - start) * 1000))
        if resp.stop_reason == "refusal":
            raise LLMError(f"model declined the '{task}' request")
        text = "".join(getattr(b, "text", "") for b in resp.content if b.type == "text")
        calls = [ToolCall(id=b.id, name=b.name, input=dict(b.input)) for b in resp.content if b.type == "tool_use"]
        return ChatResult(
            text=text,
            tool_calls=calls,
            stop_reason=str(resp.stop_reason),
            assistant_content=self._content_dicts(resp),
            usages=[usage],
        )

    def healthcheck(self) -> tuple[bool, str]:
        try:
            self._client.models.retrieve(self.settings.model)
            self._client.models.retrieve(self.settings.fast_model)
        except Exception as exc:
            return False, f"Anthropic check failed: {redact(str(exc))[:200]}"
        return True, f"models {self.settings.model} and {self.settings.fast_model} reachable"


class _RetryWithAuto(Exception):
    """Internal signal: forced tool choice unsupported; retry with auto."""
