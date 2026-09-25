"""Free-tier LLM providers (Google Gemini, NVIDIA NIM, Groq) through their OpenAI-compatible endpoints.

The agents speak Anthropic-style messages (content blocks with ``tool_use`` / ``tool_result``).
This client translates them to OpenAI chat-completions format on the way out and back into
Anthropic-style blocks on the way in, so the researcher loop, critic and writer need no changes.

Free tiers are rate limited, so every call goes through a per-provider throttle (``*_RPM``) and
429/5xx responses are retried with backoff (honouring ``Retry-After``). Structured output uses a
forced function call; when a model ignores tools and answers in text, a JSON object in that text is
accepted instead. Validation errors are fed back and retried up to two times.

:class:`FallbackClient` chains provider/model pairs, best first: when one cannot serve a call (quota
exhausted, overloaded, outage) the same call goes to the next, and the failed one is skipped for a
cool-down period so later calls do not wait on it again.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, ClassVar, TypeVar

import requests
from pydantic import BaseModel, ValidationError

from fieldnote.config import ProviderSettings, Settings
from fieldnote.costs import CostTracker
from fieldnote.llm.base import (
    ChatResult,
    LLMClient,
    LLMError,
    LLMUnavailableError,
    Tier,
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
MAX_HTTP_RETRIES = 4  # rate limits (429) usually clear within a minute
MAX_SERVER_ERROR_RETRIES = 2  # an overloaded model (503) rarely recovers within seconds: move on sooner
# A hung request is retried once, then the call fails so a FallbackClient can move to the next provider.
MAX_TIMEOUT_RETRIES = 1
REQUEST_TIMEOUT_S = 120
MAX_BACKOFF_S = 60.0
# A model whose daily free-tier quota is used up is skipped for this long, so a FallbackClient moves on
# to the next provider immediately instead of retrying a quota that will not recover until tomorrow.
DAILY_QUOTA_COOLDOWN_S = 3600.0
# How long a fallback chain skips a model after rate limiting or an outage outlasted the retries.
UNAVAILABLE_COOLDOWN_S = 600.0
# Wrong key or unknown model: will not fix itself during a run.
CONFIG_COOLDOWN_S = 3600.0
_RETRY_DELAY_RE = re.compile(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"')
# JSON-schema keywords that some OpenAI-compatible backends reject in function declarations.
# Pydantic still validates the full schema on the way back, so dropping them loses nothing.
_UNSUPPORTED_SCHEMA_KEYS = {"title", "default", "examples", "format", "pattern", "additionalProperties", "$schema"}


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    base_url: str
    key_env: str
    model_env: str
    signup_url: str


PROVIDERS: dict[str, ProviderSpec] = {
    "gemini": ProviderSpec(
        name="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        key_env="GEMINI_API_KEY",
        model_env="GEMINI_MODELS",
        signup_url="https://aistudio.google.com/apikey",
    ),
    "nvidia": ProviderSpec(
        name="nvidia",
        base_url="https://integrate.api.nvidia.com/v1",
        key_env="NVIDIA_API_KEY",
        model_env="NVIDIA_MODELS",
        signup_url="https://build.nvidia.com",
    ),
    "groq": ProviderSpec(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        key_env="GROQ_API_KEY",
        model_env="GROQ_MODELS",
        signup_url="https://console.groq.com/keys",
    ),
}


class _Throttle:
    """Minimum spacing between calls to one provider, shared across threads."""

    _locks: ClassVar[dict[str, threading.Lock]] = {}
    _last: ClassVar[dict[str, float]] = {}

    def __init__(self, provider: str, rpm: int) -> None:
        self.provider = provider
        self.interval = 60.0 / max(1, rpm)
        self._lock = self._locks.setdefault(provider, threading.Lock())

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            due = self._last.get(self.provider, 0.0) + self.interval
            if due > now:
                time.sleep(due - now)
            self._last[self.provider] = time.monotonic()


def clean_schema(node: Any) -> Any:
    """Drop unsupported keywords and collapse ``anyOf: [X, null]`` to ``X``."""
    if isinstance(node, list):
        return [clean_schema(v) for v in node]
    if not isinstance(node, dict):
        return node
    any_of = node.get("anyOf")
    if isinstance(any_of, list):
        non_null = [v for v in any_of if not (isinstance(v, dict) and v.get("type") == "null")]
        if len(non_null) == 1:
            merged = {**{k: v for k, v in node.items() if k != "anyOf"}, **non_null[0]}
            return clean_schema(merged)
    out: dict[str, Any] = {}
    for k, v in node.items():
        if k in _UNSUPPORTED_SCHEMA_KEYS:
            continue
        if k == "properties" and isinstance(v, dict):
            # Property *names* may collide with keywords (e.g. a field called "title"); keep them all.
            out[k] = {name: clean_schema(sub) for name, sub in v.items()}
        else:
            out[k] = clean_schema(v)
    return out


def _tool_api(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": clean_schema(tool.input_schema),
        },
    }


def _block_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") if isinstance(b, dict) else str(b)
            for b in content
            if not isinstance(b, dict) or b.get("type") == "text"
        )
    return str(content)


def to_openai_messages(system: str, messages: list[dict[str, Any]], provider: str) -> list[dict[str, Any]]:
    """Translate Anthropic-style messages into OpenAI chat-completions messages."""
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "assistant":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            calls = []
            for b in content:
                if b.get("type") != "tool_use":
                    continue
                call: dict[str, Any] = {
                    "id": b["id"],
                    "type": "function",
                    "function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}), ensure_ascii=False)},
                }
                # Provider-specific data (e.g. Gemini thought signatures) is echoed back only to the
                # provider that produced it.
                if b.get("provider") == provider and b.get("extra_content"):
                    call["extra_content"] = b["extra_content"]
                calls.append(call)
            entry: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                entry["tool_calls"] = calls
            elif not text:
                entry["content"] = "(no output)"
            out.append(entry)
            continue
        texts = []
        for b in content:
            if b.get("type") == "tool_result":
                body = _block_text(b.get("content", ""))
                if b.get("is_error"):
                    body = f"ERROR: {body}"
                out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": body})
            elif b.get("type") == "text":
                texts.append(b.get("text", ""))
        if texts:
            out.append({"role": "user", "content": "\n".join(texts)})
    return out


_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def json_from_text(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a text answer (fenced or bare)."""
    candidates = [m.group(1) for m in _JSON_FENCE.finditer(text)]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for c in candidates:
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def model_extra_body(provider: str, model: str) -> dict[str, Any]:
    """Provider/model-specific request fields.

    NVIDIA-hosted Nemotron models reason ("think") by default; with forced function calls that mode was
    measured failing with HTTP 500 or running for minutes, while ``enable_thinking: false`` answered the
    same extraction correctly in under a second. FieldNote's critic checks outputs anyway.
    """
    if provider == "nvidia" and "nemotron" in model.lower():
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return {}


def is_daily_quota_error(text: str) -> bool:
    """True when a 429 body says a per-day quota is exhausted (e.g. Gemini's ``...PerDay...`` quota ids)."""
    low = text.lower()
    return "perday" in low or "per day" in low or "requests per day" in low or "daily limit" in low


def retry_delay_hint(resp: Any) -> float | None:
    """Seconds to wait from ``Retry-After`` or Gemini's ``retryDelay`` detail, if present."""
    header = resp.headers.get("Retry-After") if getattr(resp, "headers", None) else None
    if header and header.replace(".", "", 1).isdigit():
        return float(header)
    m = _RETRY_DELAY_RE.search(getattr(resp, "text", "") or "")
    return float(m.group(1)) if m else None


class OpenAICompatClient(LLMClient):
    """One OpenAI-compatible provider (``gemini`` or ``nvidia``)."""

    is_mock = False

    def __init__(
        self,
        settings: Settings,
        provider: str,
        *,
        cache: LLMCacheStore | None = None,
        cost: CostTracker | None = None,
        session: requests.Session | None = None,
        models: tuple[str, str] | None = None,
    ) -> None:
        super().__init__(settings, cache=cache, cost=cost)
        if provider not in PROVIDERS:
            raise ValueError(f"unknown provider {provider!r}")
        self.spec = PROVIDERS[provider]
        self.name = provider
        self.provider = provider
        conf: ProviderSettings = settings.provider(provider)
        if not conf.api_key:
            raise LLMError(f"{self.spec.key_env} is not set")
        self._key = conf.api_key
        # (main, fast) models for this link of the chain; default: the provider's best pair.
        self._models = models or conf.model_pairs()[0]
        self._throttle = _Throttle(provider, conf.rpm)
        self._http = session or requests.Session()
        self._forced_tool_supported: dict[str, bool] = {}
        # model -> (monotonic time until which it is skipped, reason)
        self._skip_until: dict[str, tuple[float, str]] = {}

    def model_for(self, tier: Tier) -> str:
        return self._models[1] if tier == "fast" else self._models[0]

    @property
    def label(self) -> str:
        return f"{self.provider}:{self._models[0]}"

    # ---- HTTP ----------------------------------------------------------------------------------
    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.spec.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        model = str(body.get("model", ""))
        skip = self._skip_until.get(model)
        if skip is not None and time.monotonic() < skip[0]:
            raise LLMUnavailableError(f"{self.name} model {model} skipped for now: {skip[1]}")
        delay = 2.0
        timeouts = 0
        server_errors = 0
        for attempt in range(MAX_HTTP_RETRIES + 1):
            self._throttle.wait()
            try:
                resp = self._http.post(url, headers=headers, json=body, timeout=REQUEST_TIMEOUT_S)
            except requests.RequestException as exc:
                timeouts += isinstance(exc, requests.Timeout)
                if attempt == MAX_HTTP_RETRIES or timeouts > MAX_TIMEOUT_RETRIES:
                    raise LLMUnavailableError(
                        f"network error talking to {self.name} ({model}): {redact(str(exc))[:200]}",
                        UNAVAILABLE_COOLDOWN_S,
                    ) from exc
                time.sleep(delay)
                delay = min(delay * 2, MAX_BACKOFF_S)
                continue
            if resp.status_code == 200:
                data = resp.json()
                if not isinstance(data, dict) or not data.get("choices"):
                    raise LLMError(f"{self.name} returned no choices")
                return data
            detail = redact(resp.text)[:400]
            if resp.status_code == 429 and is_daily_quota_error(resp.text or ""):
                self._skip_until[model] = (time.monotonic() + DAILY_QUOTA_COOLDOWN_S, "daily quota exhausted")
                log.warning("%s: daily quota for %s is exhausted; skipping it for an hour", self.name, model)
                raise LLMUnavailableError(f"{self.name} daily quota exhausted for {model}", DAILY_QUOTA_COOLDOWN_S)
            server_errors += resp.status_code >= 500
            retryable = resp.status_code == 429 or (
                resp.status_code in (500, 502, 503, 504) and server_errors <= MAX_SERVER_ERROR_RETRIES
            )
            if retryable and attempt < MAX_HTTP_RETRIES:
                hint = retry_delay_hint(resp)
                wait = hint if hint is not None else delay
                log.info("%s returned %s; retrying in %.0fs", self.name, resp.status_code, wait)
                time.sleep(min(wait, MAX_BACKOFF_S))
                delay = min(delay * 2, MAX_BACKOFF_S)
                continue
            if resp.status_code in (401, 403):
                raise LLMUnavailableError(
                    f"{self.name} refused the request for {model} (HTTP {resp.status_code}); check {self.spec.key_env}",
                    CONFIG_COOLDOWN_S,
                )
            if resp.status_code == 404:
                raise LLMUnavailableError(
                    f"{self.name} model '{model}' not found; check {self.spec.model_env}", CONFIG_COOLDOWN_S
                )
            if resp.status_code == 413:
                # e.g. a free tier's tokens-per-minute cap is smaller than this prompt: only this call fails.
                raise LLMUnavailableError(f"{self.name} ({model}) request too large: {detail}")
            if (
                resp.status_code == 400
                and body.get("tool_choice") not in ("auto", None)
                and ("tool" in detail.lower() or "function" in detail.lower())
            ):
                raise _RetryWithAuto(detail)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise LLMUnavailableError(
                    f"{self.name} ({model}) unavailable after retries (HTTP {resp.status_code}): {detail}",
                    UNAVAILABLE_COOLDOWN_S,
                )
            raise LLMError(f"{self.name} API error {resp.status_code}: {detail}")
        raise LLMError(f"{self.name} did not respond after retries")  # pragma: no cover - loop exits above

    def _complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
        force_tool: str | None,
        max_tokens: int,
    ) -> tuple[dict[str, Any], Usage]:
        forced_ok = self._forced_tool_supported.get(model, True)
        sys_text = system
        if force_tool and not forced_ok:
            sys_text = f"{system}\n\nYou must now call the `{force_tool}` function."
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "messages": to_openai_messages(sys_text, messages, self.name),
            **model_extra_body(self.provider, model),
        }
        if tools:
            body["tools"] = [_tool_api(t) for t in tools]
            body["tool_choice"] = (
                {"type": "function", "function": {"name": force_tool}} if (force_tool and forced_ok) else "auto"
            )
        start = time.perf_counter()
        try:
            data = self._post(body)
        except _RetryWithAuto:
            log.info("%s model %s rejects forced tool_choice; switching to auto + instruction", self.name, model)
            self._forced_tool_supported[model] = False
            body["tool_choice"] = "auto"
            body["messages"] = to_openai_messages(
                f"{system}\n\nYou must now call the `{force_tool}` function.", messages, self.name
            )
            data = self._post(body)
        u = data.get("usage") or {}
        usage = Usage(
            model=model,
            input_tokens=int(u.get("prompt_tokens", 0) or 0),
            output_tokens=int(u.get("completion_tokens", 0) or 0),
            latency_ms=int((time.perf_counter() - start) * 1000),
            free=True,  # free tiers: recorded at $0, so the run budget does not limit these calls
        )
        return data["choices"][0], usage

    def _to_result(self, choice: dict[str, Any], usage: Usage) -> ChatResult:
        msg = choice.get("message") or {}
        text = msg.get("content") or ""
        if isinstance(text, list):
            text = _block_text(text)
        blocks: list[dict[str, Any]] = []
        if text:
            blocks.append({"type": "text", "text": text})
        calls: list[ToolCall] = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError:
                args = {"_unparseable_arguments": str(raw_args)[:2000]}
            if not isinstance(args, dict):
                args = {"value": args}
            call_id = (
                tc.get("id") or "call_" + hashlib.sha256(f"{i}:{fn.get('name')}:{raw_args}".encode()).hexdigest()[:16]
            )
            calls.append(ToolCall(id=call_id, name=str(fn.get("name", "")), input=args))
            block: dict[str, Any] = {
                "type": "tool_use",
                "id": call_id,
                "name": fn.get("name", ""),
                "input": args,
                "provider": self.name,
            }
            if tc.get("extra_content"):
                block["extra_content"] = tc["extra_content"]
            blocks.append(block)
        finish = str(choice.get("finish_reason") or "")
        stop = "tool_use" if calls else ("max_tokens" if finish == "length" else "end_turn")
        return ChatResult(text=text, tool_calls=calls, stop_reason=stop, assistant_content=blocks, usages=[usage])

    # ---- LLMClient implementation --------------------------------------------------------------
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
            choice, usage = self._complete(
                model=model, system=system, messages=messages, tools=[tool], force_tool=tool.name, max_tokens=max_tokens
            )
            usages.append(usage)
            res = self._to_result(choice, usage)
            call = next((c for c in res.tool_calls if c.name == tool.name), None)
            data = call.input if call is not None else json_from_text(res.text)
            if data is None:
                last_error = "the model returned neither a function call nor JSON"
                messages = [
                    *messages,
                    {
                        "role": "assistant",
                        "content": res.assistant_content or [{"type": "text", "text": "(no output)"}],
                    },
                    {"role": "user", "content": f"Call the `{tool.name}` function now with the complete result."},
                ]
                continue
            try:
                return schema.model_validate(data), usages
            except ValidationError as exc:
                last_error = str(exc)
                log.info("structured output for %s failed validation on %s; retrying with feedback", task, self.name)
                feedback = f"Schema validation failed:\n{exc}\nCall `{tool.name}` again with corrected input."
                if call is not None:
                    messages = [
                        *messages,
                        {"role": "assistant", "content": res.assistant_content},
                        {
                            "role": "user",
                            "content": [
                                {"type": "tool_result", "tool_use_id": call.id, "is_error": True, "content": feedback}
                            ],
                        },
                    ]
                else:
                    messages = [
                        *messages,
                        {"role": "assistant", "content": res.text or "(no output)"},
                        {"role": "user", "content": feedback},
                    ]
        raise LLMError(f"structured output for '{task}' failed on {self.name} after retries: {last_error[:500]}")

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
        choice, usage = self._complete(
            model=model, system=system, messages=messages, tools=tools, force_tool=force_tool, max_tokens=max_tokens
        )
        return self._to_result(choice, usage)

    def healthcheck(self) -> tuple[bool, str]:
        try:
            resp = self._http.get(
                f"{self.spec.base_url}/models", headers={"Authorization": f"Bearer {self._key}"}, timeout=30
            )
        except requests.RequestException as exc:
            return False, f"{self.name} unreachable: {redact(str(exc))[:200]}"
        if resp.status_code in (401, 403):
            return False, f"{self.name} rejected the key ({self.spec.key_env})"
        if resp.status_code != 200:
            return False, f"{self.name} /models returned {resp.status_code}"
        ids = {str(m.get("id", "")).removeprefix("models/") for m in resp.json().get("data", [])}
        missing = [m for m in dict.fromkeys(self._models) if ids and m not in ids]
        if missing:
            return False, f"{self.name}: model(s) not available: {', '.join(missing)} (set {self.spec.model_env})"
        return True, f"{self.name} {' / '.join(dict.fromkeys(self._models))} available"


class FallbackClient(LLMClient):
    """Try provider/model links in order (best first); a call that fails on one goes to the next.

    A link that is unavailable (quota, rate limit, outage, auth) is skipped for the cool-down its error
    asks for, so later calls go straight to a working link. If every link is cooling down, all are tried
    again in order rather than failing without a request.
    """

    is_mock = False

    def __init__(
        self,
        settings: Settings,
        clients: list[LLMClient],
        *,
        cache: LLMCacheStore | None = None,
        cost: CostTracker | None = None,
    ) -> None:
        super().__init__(settings, cache=cache, cost=cost)
        if not clients:
            raise ValueError("FallbackClient needs at least one client")
        self.clients = clients
        self.name = "+".join(dict.fromkeys(c.name for c in clients))
        self._cool_until = [0.0] * len(clients)

    def model_for(self, tier: Tier) -> str:
        # Composite name: part of the cache key, and split back per provider in the calls below.
        return "|".join(c.model_for(tier) for c in self.clients)

    @staticmethod
    def _label(client: LLMClient) -> str:
        return str(getattr(client, "label", client.name))

    def _run(self, model: str, fn: Any) -> Any:
        models = model.split("|")
        now = time.monotonic()
        order = [i for i in range(len(self.clients)) if self._cool_until[i] <= now]
        if not order:  # everything is cooling down: try them all again rather than fail without asking
            order = list(range(len(self.clients)))
        errors = []
        for i in order:
            client = self.clients[i]
            try:
                result = fn(client, models[i])
            except LLMUnavailableError as exc:
                errors.append(f"{self._label(client)}: {exc}")
                if exc.cooldown > 0:
                    self._cool_until[i] = time.monotonic() + exc.cooldown
                    log.warning(
                        "%s unavailable (%s); skipping it for %d min",
                        self._label(client),
                        str(exc)[:160],
                        int(exc.cooldown // 60),
                    )
                else:
                    log.warning(
                        "%s could not serve this call (%s); trying the next model", self._label(client), str(exc)[:160]
                    )
                continue
            except LLMError as exc:
                errors.append(f"{self._label(client)}: {exc}")
                log.warning("%s failed (%s); trying the next model", self._label(client), str(exc)[:200])
                continue
            self._cool_until[i] = 0.0
            return result
        raise LLMError("all models failed: " + " | ".join(errors))

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
        result: tuple[T, list[Usage]] = self._run(
            model,
            lambda c, m: c._structured_impl(
                task=task, model=m, system=system, prompt=prompt, schema=schema, payload=payload, max_tokens=max_tokens
            ),
        )
        return result

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
        result: ChatResult = self._run(
            model,
            lambda c, m: c._chat_impl(
                task=task,
                model=m,
                system=system,
                messages=messages,
                tools=tools,
                force_tool=force_tool,
                payload=payload,
                max_tokens=max_tokens,
            ),
        )
        return result

    def healthcheck(self) -> tuple[bool, str]:
        results = [c.healthcheck() for c in self.clients]
        ok = any(r[0] for r in results)
        return ok, "; ".join(r[1] for r in results)


class _RetryWithAuto(Exception):
    """Internal signal: forced tool choice unsupported; retry with auto."""
