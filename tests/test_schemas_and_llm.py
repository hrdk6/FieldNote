from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from fieldnote.config import get_settings
from fieldnote.costs import BudgetExceededError, CostTracker, estimate_cost, price_for
from fieldnote.llm.anthropic_client import AnthropicClient
from fieldnote.llm.base import inline_refs, make_llm
from fieldnote.llm.cache import LLMCacheStore
from fieldnote.llm.mock_client import MockClient
from fieldnote.llm.schemas import AnalystOutput, ClaimDraft, FindingDraft, Rating, ResearchOutput, RouterOutput


def test_claim_schema_validation() -> None:
    ok = ClaimDraft(ref="c1", type="fact", text=" X did Y ", source_document_ids=[1], excerpt="X did Y")
    assert ok.text == "X did Y"
    with pytest.raises(ValidationError):
        ClaimDraft(ref="c1", type="fact", text="   ")
    with pytest.raises(ValidationError):
        ClaimDraft(ref="c1", type="rumour", text="x")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Rating(value=7, rationale="too high")
    r = Rating(value=3, rationale="ok")
    with pytest.raises(ValidationError):
        FindingDraft(
            title="t",
            category="risk",
            what_happened="w",
            why_it_matters="y",
            evidence_claim_ids=[],
            recommended_action="a",
            how_to_execute="h",
            market_size=r,
            competitor_gap=r,
            effort=r,
        )
    parsed = ResearchOutput.model_validate(
        {
            "claims": [
                {
                    "ref": "c1",
                    "type": "estimate",
                    "text": "Estimate: 5",
                    "derivation": {"method": "m", "op": "sum", "operands": [2, 3], "result": 5},
                }
            ]
        }
    )
    assert parsed.claims[0].derivation is not None and parsed.claims[0].derivation.result == 5


def test_inline_refs_removes_defs() -> None:
    schema = inline_refs(AnalystOutput.model_json_schema())
    assert "$defs" not in str(schema) and "$ref" not in str(schema)
    assert schema["properties"]["findings"]["items"]["properties"]["market_size"]["properties"]["value"]["maximum"] == 5


def test_costs_and_budget() -> None:
    assert price_for("claude-sonnet-5") == (2.0, 10.0)
    assert price_for("claude-haiku-4-5-20251001") == (1.0, 5.0)
    assert estimate_cost("claude-sonnet-5", 1_000_000, 100_000) == pytest.approx(3.0)
    t = CostTracker(budget_usd=0.01)
    from fieldnote.costs import UsageEvent

    t.record(UsageEvent("x", "claude-sonnet-5", 10_000, 0, 0.02, 1, False))
    with pytest.raises(BudgetExceededError):
        t.check()


def test_make_llm_picks_mock_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIELDNOTE_LLM", "auto")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert isinstance(make_llm(get_settings()), MockClient)
    monkeypatch.setenv("FIELDNOTE_LLM", "anthropic")
    assert isinstance(make_llm(get_settings()), MockClient)  # falls back with a warning


def test_cache_makes_calls_deterministic_and_free(db) -> None:  # type: ignore[no-untyped-def]
    cost = CostTracker()
    llm = MockClient(get_settings(), cache=LLMCacheStore(db, "t"), cost=cost)
    kw: dict[str, Any] = {
        "task": "ask_router",
        "system": "s",
        "prompt": "What changed?",
        "schema": RouterOutput,
        "payload": {"question": "What changed in pricing?", "aliases": {}},
    }
    a = llm.structured(**kw)
    b = llm.structured(**kw)
    assert a == b
    assert len(llm.calls) == 1, "second call is served from the cache"
    assert cost.cached_calls == 1
    assert llm.cache.flush() == 1


def test_mock_budget_is_enforced(db) -> None:  # type: ignore[no-untyped-def]
    cost = CostTracker(budget_usd=0.001)
    from fieldnote.costs import UsageEvent

    cost.record(UsageEvent("prior", "claude-sonnet-5", 0, 0, 0.01, 1, False))
    llm = MockClient(get_settings(), cost=cost)
    with pytest.raises(BudgetExceededError):
        llm.structured(task="ask_router", system="s", prompt="new", schema=RouterOutput, payload={"question": "q"})


# ---- AnthropicClient with a fake SDK (no network) ----------------------------------------------


class _Block(SimpleNamespace):
    def model_dump(self, **_: Any) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def _resp(tool_input: dict[str, Any] | None, name: str = "emit_routeroutput") -> SimpleNamespace:
    content = (
        [_Block(type="tool_use", id="tu_1", name=name, input=tool_input)]
        if tool_input is not None
        else [_Block(type="text", text="hi")]
    )
    return SimpleNamespace(
        content=content,
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=100, output_tokens=20, cache_read_input_tokens=0, cache_creation_input_tokens=0
        ),
    )


def test_anthropic_structured_retries_with_validation_feedback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-000000000000")
    client = AnthropicClient(get_settings())
    calls: list[dict[str, Any]] = []
    responses = [
        _resp({"intent": "not-an-intent"}),
        _resp({"intent": "metrics", "entities": [], "needs_research": False}),
    ]

    def fake_create(**kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(client._client.messages, "create", fake_create)
    out = client.structured(task="ask_router", system="s", prompt="p", schema=RouterOutput, tier="fast")
    assert out.intent == "metrics"
    assert len(calls) == 2
    assert calls[0]["tool_choice"] == {"type": "tool", "name": "emit_routeroutput"}
    retry_msgs = calls[1]["messages"]
    assert retry_msgs[-1]["content"][0]["type"] == "tool_result" and retry_msgs[-1]["content"][0]["is_error"]
    assert client.cost.tokens_in == 200 and client.cost.total_cost > 0


def test_anthropic_falls_back_when_forced_tool_choice_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    import anthropic
    import httpx2 as httpx

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-000000000000")
    client = AnthropicClient(get_settings())
    seen: list[dict[str, Any]] = []

    def fake_create(**kwargs: Any) -> SimpleNamespace:
        seen.append(kwargs)
        if kwargs["tool_choice"]["type"] == "tool":
            req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            raise anthropic.BadRequestError(
                'tool_choice: type "tool" and "any" are not supported for this model.',
                response=httpx.Response(400, request=req),
                body=None,
            )
        return _resp({"intent": "general"})

    monkeypatch.setattr(client._client.messages, "create", fake_create)
    out = client.structured(task="ask_router", system="s", prompt="p", schema=RouterOutput)
    assert out.intent == "general"
    assert seen[-1]["tool_choice"] == {"type": "auto"}
