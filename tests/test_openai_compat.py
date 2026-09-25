"""Gemini / NVIDIA client tests against a fake HTTP session (no network, no real keys)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel

from fieldnote.config import get_settings
from fieldnote.costs import CostTracker
from fieldnote.llm import openai_compat
from fieldnote.llm.base import LLMError, ToolSpec, make_llm
from fieldnote.llm.mock_client import MockClient
from fieldnote.llm.openai_compat import (
    FallbackClient,
    OpenAICompatClient,
    clean_schema,
    json_from_text,
    to_openai_messages,
)


class Verdict(BaseModel):
    label: str
    score: int


class FakeResponse:
    def __init__(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = json.dumps(body)

    def json(self) -> Any:
        return self._body


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    def post(self, url: str, headers: dict[str, str], json: dict[str, Any], timeout: int) -> FakeResponse:
        self.requests.append({"url": url, "headers": headers, "body": json})
        return self.responses.pop(0)

    def get(self, url: str, headers: dict[str, str], timeout: int) -> FakeResponse:
        return FakeResponse(200, {"data": [{"id": "models/gemini-3.8-flash"}, {"id": "models/gemini-3.5-flash-lite"}]})


def _tool_reply(name: str, args: dict[str, Any], extra: dict[str, Any] | None = None) -> FakeResponse:
    call: dict[str, Any] = {
        "id": "call_1",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }
    if extra:
        call["extra_content"] = extra
    return FakeResponse(
        200,
        {
            "choices": [
                {"message": {"role": "assistant", "content": None, "tool_calls": [call]}, "finish_reason": "tool_calls"}
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        },
    )


def _text_reply(text: str) -> FakeResponse:
    return FakeResponse(
        200, {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}
    )


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(openai_compat.time, "sleep", lambda s: slept.append(s))
    openai_compat._Throttle._last.clear()
    return slept


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gem-test-key-000")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key-000")
    monkeypatch.setenv("GEMINI_RPM", "600")
    monkeypatch.setenv("NVIDIA_RPM", "600")


def _client(
    provider: str, responses: list[FakeResponse], cost: CostTracker | None = None
) -> tuple[OpenAICompatClient, FakeSession]:
    session = FakeSession(responses)
    return OpenAICompatClient(get_settings(), provider, session=session, cost=cost), session  # type: ignore[arg-type]


def test_clean_schema_collapses_nullable_and_keeps_property_names() -> None:
    schema = {
        "title": "X",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string", "title": "Title", "default": ""},
            "url": {"anyOf": [{"type": "string", "format": "uri"}, {"type": "null"}], "default": None},
        },
    }
    out = clean_schema(schema)
    assert out == {"type": "object", "properties": {"title": {"type": "string"}, "url": {"type": "string"}}}


def test_message_translation_round_trip() -> None:
    messages = [
        {"role": "user", "content": "find evidence"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Searching."},
                {
                    "type": "tool_use",
                    "id": "c1",
                    "name": "search",
                    "input": {"q": "x"},
                    "provider": "gemini",
                    "extra_content": {"google": {"thought_signature": "sig"}},
                },
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "c1", "is_error": True, "content": "bad args"}],
        },
    ]
    out = to_openai_messages("sys", messages, "gemini")
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool"]
    assert out[2]["tool_calls"][0]["function"] == {"name": "search", "arguments": '{"q": "x"}'}
    assert out[2]["tool_calls"][0]["extra_content"] == {"google": {"thought_signature": "sig"}}
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "ERROR: bad args"}
    # Another provider never receives Gemini's signature.
    assert "extra_content" not in to_openai_messages("sys", messages, "nvidia")[2]["tool_calls"][0]


def test_structured_forced_function_call_and_free_cost(keys: None) -> None:
    cost = CostTracker(budget_usd=0.01)
    client, session = _client("gemini", [_tool_reply("emit_verdict", {"label": "ok", "score": 3})], cost=cost)
    out = client.structured(task="t", system="s", prompt="p", schema=Verdict, tier="fast")
    assert out == Verdict(label="ok", score=3)
    body = session.requests[0]["body"]
    assert session.requests[0]["url"] == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert session.requests[0]["headers"]["Authorization"] == "Bearer gem-test-key-000"
    assert body["model"] == "gemini-3.5-flash-lite"
    assert body["tool_choice"] == {"type": "function", "function": {"name": "emit_verdict"}}
    assert cost.total_cost == 0.0 and cost.tokens_in == 100


def test_structured_retries_validation_then_accepts_json_text(keys: None) -> None:
    client, session = _client(
        "nvidia",
        [
            _tool_reply("emit_verdict", {"label": "ok", "score": "not-a-number"}),
            _text_reply('Here you go:\n```json\n{"label": "fixed", "score": 7}\n```'),
        ],
    )
    out = client.structured(task="t", system="s", prompt="p", schema=Verdict)
    assert out.label == "fixed" and out.score == 7
    retry_msgs = session.requests[1]["body"]["messages"]
    assert retry_msgs[-1]["role"] == "tool" and retry_msgs[-1]["content"].startswith("ERROR: Schema validation failed")
    assert session.requests[0]["url"].startswith("https://integrate.api.nvidia.com/v1/")


def test_rate_limit_retry_honours_retry_after(keys: None, no_sleep: list[float]) -> None:
    client, _ = _client(
        "gemini",
        [
            FakeResponse(429, {"error": "quota"}, {"Retry-After": "7"}),
            _tool_reply("emit_verdict", {"label": "a", "score": 1}),
        ],
    )
    assert client.structured(task="t", system="s", prompt="p", schema=Verdict).label == "a"
    assert 7.0 in no_sleep


def test_forced_tool_choice_rejected_falls_back_to_auto(keys: None) -> None:
    client, session = _client(
        "nvidia",
        [
            FakeResponse(400, {"error": "tool_choice with a named function is not supported"}),
            _tool_reply("emit_verdict", {"label": "a", "score": 1}),
        ],
    )
    client.structured(task="t", system="s", prompt="p", schema=Verdict)
    assert session.requests[1]["body"]["tool_choice"] == "auto"
    assert "must now call" in session.requests[1]["body"]["messages"][0]["content"]


def test_chat_with_tools_returns_anthropic_style_blocks(keys: None) -> None:
    client, _ = _client(
        "gemini", [_tool_reply("search", {"q": "range"}, extra={"google": {"thought_signature": "s1"}})]
    )
    tool = ToolSpec(
        name="search", description="d", input_schema={"type": "object", "properties": {"q": {"type": "string"}}}
    )
    res = client.chat_with_tools(
        task="researcher", system="s", messages=[{"role": "user", "content": "go"}], tools=[tool]
    )
    assert res.stop_reason == "tool_use"
    assert res.tool_calls[0].input == {"q": "range"}
    block = res.assistant_content[0]
    assert block["type"] == "tool_use" and block["provider"] == "gemini" and block["extra_content"]


def test_fallback_chain_moves_to_next_provider(keys: None) -> None:
    gem, _ = _client("gemini", [FakeResponse(401, {"error": "bad key"})])
    nv, _ = _client("nvidia", [_tool_reply("emit_verdict", {"label": "nv", "score": 2})])
    chain = FallbackClient(get_settings(), [gem, nv])
    assert chain.structured(task="t", system="s", prompt="p", schema=Verdict).label == "nv"
    assert chain.name == "gemini+nvidia"


def test_fallback_chain_reports_all_failures(keys: None) -> None:
    gem, _ = _client("gemini", [FakeResponse(403, {"error": "no"})])
    nv, _ = _client("nvidia", [FakeResponse(404, {"error": "no model"})])
    with pytest.raises(LLMError, match="all models failed"):
        FallbackClient(get_settings(), [gem, nv]).structured(task="t", system="s", prompt="p", schema=Verdict)


def test_make_llm_selection(monkeypatch: pytest.MonkeyPatch, keys: None) -> None:
    monkeypatch.setenv("FIELDNOTE_LLM", "auto")
    assert isinstance(make_llm(get_settings()), FallbackClient)
    assert isinstance(make_llm(get_settings(), offline=True), MockClient)
    monkeypatch.setenv("FIELDNOTE_LLM", "nvidia")
    llm = make_llm(get_settings())
    assert llm.name == "nvidia"  # one provider, its models best first
    monkeypatch.setenv("NVIDIA_MODELS", "only-one")
    assert isinstance(make_llm(get_settings()), OpenAICompatClient)  # a single model needs no chain
    monkeypatch.delenv("NVIDIA_MODELS")
    monkeypatch.setenv("FIELDNOTE_LLM", "nvidia,gemini")
    assert make_llm(get_settings()).name == "nvidia+gemini"
    monkeypatch.delenv("GEMINI_API_KEY")
    monkeypatch.delenv("NVIDIA_API_KEY")
    monkeypatch.setenv("FIELDNOTE_LLM", "auto")
    assert isinstance(make_llm(get_settings()), MockClient)


def test_json_from_text_variants() -> None:
    assert json_from_text('{"a": 1}') == {"a": 1}
    assert json_from_text("no json here") is None
    assert json_from_text('prefix ```json\n{"b": 2}\n``` suffix') == {"b": 2}


def test_healthcheck_lists_models(keys: None) -> None:
    client, _ = _client("gemini", [])
    ok, msg = client.healthcheck()
    assert ok, msg


def test_hung_provider_fails_fast_so_fallback_can_take_over(keys: None) -> None:
    class HangingSession(FakeSession):
        def post(self, url: str, headers: dict[str, str], json: dict[str, Any], timeout: int) -> FakeResponse:
            self.requests.append({"url": url})
            raise openai_compat.requests.ReadTimeout("read timed out")

    session = HangingSession([])
    gem = OpenAICompatClient(get_settings(), "gemini", session=session)  # type: ignore[arg-type]
    nv, _ = _client("nvidia", [_tool_reply("emit_verdict", {"label": "nv", "score": 1})])
    assert (
        FallbackClient(get_settings(), [gem, nv]).structured(task="t", system="s", prompt="p", schema=Verdict).label
        == "nv"
    )
    assert len(session.requests) == 1 + openai_compat.MAX_TIMEOUT_RETRIES


def test_daily_quota_skips_the_model_so_fallback_is_immediate(keys: None) -> None:
    daily = FakeResponse(
        429,
        [{"error": {"code": 429, "message": "You exceeded your current quota",
                    "details": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}}],
    )  # fmt: skip
    gem, gem_session = _client("gemini", [daily])
    nv, _ = _client(
        "nvidia",
        [
            _tool_reply("emit_verdict", {"label": "a", "score": 1}),
            _tool_reply("emit_verdict", {"label": "b", "score": 2}),
        ],
    )
    chain = FallbackClient(get_settings(), [gem, nv])
    assert chain.structured(task="t1", system="s", prompt="p1", schema=Verdict).label == "a"
    assert chain.structured(task="t2", system="s", prompt="p2", schema=Verdict).label == "b"
    assert len(gem_session.requests) == 1  # the exhausted model was not called again


def test_per_minute_limit_honours_retry_delay_hint(keys: None, no_sleep: list[float]) -> None:
    per_minute = FakeResponse(
        429,
        {"error": {"details": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"},
                               {"retryDelay": "13s"}]}},
    )  # fmt: skip
    client, session = _client("gemini", [per_minute, _tool_reply("emit_verdict", {"label": "ok", "score": 1})])
    assert client.structured(task="t", system="s", prompt="p", schema=Verdict).label == "ok"
    assert 13.0 in no_sleep and len(session.requests) == 2


def test_chain_order_is_best_model_first_then_next_provider(monkeypatch: pytest.MonkeyPatch, keys: None) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test-key-000")
    monkeypatch.setenv("GEMINI_MODELS", "gem-best, gem-good")
    monkeypatch.setenv("GEMINI_FAST_MODELS", "gem-lite")
    monkeypatch.setenv("FIELDNOTE_LLM", "auto")
    chain = make_llm(get_settings())
    assert isinstance(chain, FallbackClient) and chain.name == "gemini+nvidia+groq"
    labels = [c.label for c in chain.clients]  # type: ignore[attr-defined]
    assert labels[:2] == ["gemini:gem-best", "gemini:gem-good"]
    assert labels[2].startswith("nvidia:") and labels[-2:] == [
        "groq:openai/gpt-oss-120b",
        "groq:openai/gpt-oss-20b",
    ]
    assert [c.model_for("fast") for c in chain.clients[:2]] == ["gem-lite", "gem-lite"]
    groq = chain.clients[-2]
    assert groq.spec.base_url == "https://api.groq.com/openai/v1"  # type: ignore[attr-defined]
    # A single legacy *_MODEL variable still works.
    monkeypatch.delenv("GEMINI_MODELS")
    monkeypatch.setenv("GEMINI_MODEL", "only-this")
    assert get_settings().provider("gemini").models == ("only-this",)


def test_unavailable_model_is_skipped_during_its_cooldown(keys: None) -> None:
    overloaded = FakeResponse(503, {"error": "The model is overloaded"})
    best, best_session = _client("gemini", [overloaded, overloaded, overloaded])
    backup, _ = _client(
        "nvidia",
        [
            _tool_reply("emit_verdict", {"label": "a", "score": 1}),
            _tool_reply("emit_verdict", {"label": "b", "score": 2}),
        ],
    )
    chain = FallbackClient(get_settings(), [best, backup])
    assert chain.structured(task="t1", system="s", prompt="p1", schema=Verdict).label == "a"
    assert len(best_session.requests) == 3  # 1 try + MAX_SERVER_ERROR_RETRIES, then give up on it
    assert chain.structured(task="t2", system="s", prompt="p2", schema=Verdict).label == "b"
    assert len(best_session.requests) == 3  # cooling down: not asked again


def test_when_every_model_is_cooling_down_they_are_tried_again(keys: None) -> None:
    gem, _ = _client("gemini", [_tool_reply("emit_verdict", {"label": "back", "score": 1})])
    nv, _ = _client("nvidia", [])
    chain = FallbackClient(get_settings(), [gem, nv])
    chain._cool_until = [float("inf"), float("inf")]
    assert chain.structured(task="t", system="s", prompt="p", schema=Verdict).label == "back"
    assert chain._cool_until[0] == 0.0  # success clears the cool-down
