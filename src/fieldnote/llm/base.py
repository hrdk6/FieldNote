"""Provider-neutral LLM interface.

Every LLM call in FieldNote goes through :class:`LLMClient`, which adds three cross-cutting
behaviours on top of a provider implementation:

* a response cache (key = hash of model + prompt + schema),
* the per-run cost tracker (raises :class:`BudgetExceededError` once the budget is spent),
* structured output validated against a Pydantic schema.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ValidationError

from fieldnote.config import Settings
from fieldnote.costs import CostTracker, UsageEvent, estimate_cost
from fieldnote.llm.cache import LLMCacheStore, cache_key
from fieldnote.logging_setup import get_logger

log = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)
Tier = Literal["main", "fast"]


class LLMError(Exception):
    """An LLM call failed in a way the caller should degrade around."""


class LLMUnavailableError(LLMError):
    """The provider/model could not serve the call (quota, rate limit, outage, auth, unknown model).

    ``cooldown`` (seconds) tells a fallback chain how long to skip this model before trying it again;
    0 means "only this call failed" (for example a request too large for a free tier's per-minute cap).
    """

    def __init__(self, message: str, cooldown: float = 0.0) -> None:
        super().__init__(message)
        self.cooldown = cooldown


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]

    def to_api(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class Usage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    latency_ms: int = 0
    free: bool = False


@dataclass
class ChatResult:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str
    assistant_content: list[dict[str, Any]]
    usages: list[Usage] = field(default_factory=list)


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline ``$ref``/``$defs`` from a Pydantic JSON schema so any tool-schema consumer accepts it."""
    defs = schema.get("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                target = dict(defs.get(name, {}))
                merged = {**resolve(target), **{k: resolve(v) for k, v in node.items() if k != "$ref"}}
                return merged
            return {k: resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    out = resolve(schema)
    out.pop("title", None)
    return out


def schema_tool(schema: type[BaseModel], task: str) -> ToolSpec:
    name = f"emit_{schema.__name__.lower()}"[:64]
    return ToolSpec(
        name=name,
        description=f"Return the result of the '{task}' task as structured data. Always call this tool.",
        input_schema=inline_refs(schema.model_json_schema()),
    )


class LLMClient(ABC):
    name: str = "base"
    is_mock: bool = False
    cache_namespace: str = "v1"

    def __init__(
        self,
        settings: Settings,
        *,
        cache: LLMCacheStore | None = None,
        cost: CostTracker | None = None,
    ) -> None:
        self.settings = settings
        self.cache = cache or LLMCacheStore(None)
        self.cost = cost or CostTracker()

    # ---- models ------------------------------------------------------------------------------
    def model_for(self, tier: Tier) -> str:
        return self.settings.fast_model if tier == "fast" else self.settings.model

    # ---- structured output -------------------------------------------------------------------
    def structured(
        self,
        *,
        task: str,
        system: str,
        prompt: str,
        schema: type[T],
        tier: Tier = "main",
        payload: dict[str, Any] | None = None,
        max_tokens: int = 4096,
    ) -> T:
        model = self.model_for(tier)
        key = cache_key(
            ns=f"{self.name}:{self.cache_namespace}",
            kind="structured",
            model=model,
            system=system,
            prompt=prompt,
            schema=schema.model_json_schema(),
        )
        hit = self.cache.get(key)
        if hit is not None:
            try:
                obj = schema.model_validate_json(hit)
                self._record(task, Usage(model=model), cached=True)
                return obj
            except ValidationError:
                log.debug("cache entry for %s failed validation; recomputing", task)
        self.cost.check()
        start = time.perf_counter()
        obj, usages = self._structured_impl(
            task=task,
            model=model,
            system=system,
            prompt=prompt,
            schema=schema,
            payload=payload or {},
            max_tokens=max_tokens,
        )
        elapsed = int((time.perf_counter() - start) * 1000)
        for i, u in enumerate(usages):
            if u.latency_ms == 0 and i == len(usages) - 1:
                u.latency_ms = elapsed
            self._record(task, u, cached=False)
        tin = sum(u.input_tokens for u in usages)
        tout = sum(u.output_tokens for u in usages)
        self.cache.put(key, obj.model_dump_json(), model=model, task=task, input_tokens=tin, output_tokens=tout)
        return obj

    # ---- tool-use chat (agent loops) ---------------------------------------------------------
    def chat_with_tools(
        self,
        *,
        task: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
        tier: Tier = "main",
        force_tool: str | None = None,
        payload: dict[str, Any] | None = None,
        max_tokens: int = 4096,
    ) -> ChatResult:
        model = self.model_for(tier)
        key = cache_key(
            ns=f"{self.name}:{self.cache_namespace}",
            kind="chat",
            model=model,
            system=system,
            messages=messages,
            tools=[t.to_api() for t in tools],
            force=force_tool,
        )
        hit = self.cache.get(key)
        if hit is not None:
            data = json.loads(hit)
            self._record(task, Usage(model=model), cached=True)
            return ChatResult(
                text=data["text"],
                tool_calls=[ToolCall(**c) for c in data["tool_calls"]],
                stop_reason=data["stop_reason"],
                assistant_content=data["assistant_content"],
            )
        self.cost.check()
        start = time.perf_counter()
        result = self._chat_impl(
            task=task,
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            force_tool=force_tool,
            payload=payload or {},
            max_tokens=max_tokens,
        )
        elapsed = int((time.perf_counter() - start) * 1000)
        for u in result.usages:
            if u.latency_ms == 0:
                u.latency_ms = elapsed
            self._record(task, u, cached=False)
        blob = json.dumps(
            {
                "text": result.text,
                "tool_calls": [c.__dict__ for c in result.tool_calls],
                "stop_reason": result.stop_reason,
                "assistant_content": result.assistant_content,
            },
            ensure_ascii=False,
        )
        self.cache.put(
            key,
            blob,
            model=model,
            task=task,
            input_tokens=sum(u.input_tokens for u in result.usages),
            output_tokens=sum(u.output_tokens for u in result.usages),
        )
        return result

    # ---- helpers -----------------------------------------------------------------------------
    def _record(self, task: str, usage: Usage, *, cached: bool) -> None:
        cost = (
            0.0
            if cached or self.is_mock or usage.free
            else estimate_cost(
                usage.model, usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.cache_write_tokens
            )
        )
        self.cost.record(
            UsageEvent(
                task=task,
                model=usage.model,
                input_tokens=0 if cached else usage.input_tokens,
                output_tokens=0 if cached else usage.output_tokens,
                cost_usd=cost,
                latency_ms=usage.latency_ms,
                cached=cached,
            )
        )

    @abstractmethod
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
    ) -> tuple[T, list[Usage]]: ...

    @abstractmethod
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
    ) -> ChatResult: ...

    def healthcheck(self) -> tuple[bool, str]:
        return True, f"{self.name} client ready"


def make_llm(
    settings: Settings,
    *,
    offline: bool = False,
    cache: LLMCacheStore | None = None,
    cost: CostTracker | None = None,
    force: str | None = None,
) -> LLMClient:
    """Pick the LLM implementation.

    ``FIELDNOTE_LLM`` (or ``force``) may be ``auto`` / ``mock`` / ``anthropic`` / ``gemini`` /
    ``nvidia`` / ``groq``, or a comma-separated fallback chain such as ``gemini,nvidia,groq``. In ``auto``
    mode every provider with a key is chained in the order Anthropic, Gemini, NVIDIA, Groq. Each free
    provider contributes one link per model pair in its ``*_MODELS`` list (best first), so the chain runs
    e.g. gemini-3.8-flash -> gemini-3.7-flash -> ... -> NVIDIA -> Groq. With no key (or an ``--offline``
    run in ``auto`` mode) the deterministic MockClient keeps the whole system working.
    """
    from fieldnote.llm.mock_client import MockClient

    choice = (force or settings.llm_provider or "auto").lower().replace(" ", "")
    if choice == "mock" or (choice == "auto" and offline):
        return MockClient(settings, cache=cache, cost=cost)
    available = {"anthropic": settings.has_anthropic} | {p.name: p.configured for p in settings.free_providers}
    wanted = list(available) if choice == "auto" else [p for p in choice.split(",") if p]
    unknown = [p for p in wanted if p not in available]
    if unknown:
        log.warning("unknown FIELDNOTE_LLM provider(s) %s; valid: auto, mock, %s", unknown, ", ".join(available))
    chain: list[LLMClient] = []
    for provider in wanted:
        if provider not in available:
            continue
        if not available[provider]:
            if choice != "auto":
                log.warning("FIELDNOTE_LLM includes %s but its API key is not set; skipping it", provider)
            continue
        chain.extend(_provider_clients(provider, settings, cache=cache, cost=cost))
    if not chain:
        return MockClient(settings, cache=cache, cost=cost)
    if len(chain) == 1:
        return chain[0]
    from fieldnote.llm.openai_compat import FallbackClient

    return FallbackClient(settings, chain, cache=cache, cost=cost)


def _provider_clients(
    provider: str, settings: Settings, *, cache: LLMCacheStore | None, cost: CostTracker | None
) -> list[LLMClient]:
    if provider == "anthropic":
        from fieldnote.llm.anthropic_client import AnthropicClient

        return [AnthropicClient(settings, cache=cache, cost=cost)]
    from fieldnote.llm.openai_compat import OpenAICompatClient

    return [
        OpenAICompatClient(settings, provider, cache=cache, cost=cost, models=pair)
        for pair in settings.provider(provider).model_pairs()
    ]
