"""Deterministic, fixture-compatible MockClient.

Used by tests, `fieldnote demo`, `fieldnote run --offline`, and whenever no API key is configured.
Every task has a rule-based handler that reads the structured ``payload`` the caller supplies next to
the prompt (the real client ignores the payload and reads the prompt). Canned responses can also be
injected per task (``responses``) or loaded from ``fixtures/mock_llm_overrides.json``.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel

from fieldnote.builder.heuristic import heuristic_draft
from fieldnote.config import Settings, fixtures_dir
from fieldnote.costs import CostTracker
from fieldnote.heuristics import (
    best_quote,
    classify_change_rules,
    detect_aspect,
    detect_language,
    extract_action_items_heuristic,
    extract_claimed_value,
    extract_conditions,
    extract_reported_value,
    route_intent,
    sentiment_score,
)
from fieldnote.llm import mock_agents
from fieldnote.llm.base import ChatResult, LLMClient, ToolSpec, Usage
from fieldnote.llm.cache import LLMCacheStore
from fieldnote.textutil import find_entities, unwrap_untrusted

T = TypeVar("T", bound=BaseModel)

MOCK_VERSION = "mock-2026.09.5"

Handler = Callable[[dict[str, Any]], dict[str, Any]]


def _voice_tagging(p: dict[str, Any]) -> dict[str, Any]:
    items = []
    for d in p.get("documents", []):
        text = unwrap_untrusted(d.get("text", ""))
        items.append(
            {
                "document_id": d["id"],
                "entities": find_entities(text, p.get("aliases", {})),
                "aspect": detect_aspect(text, p.get("aspect_terms", {})),
                "sentiment": sentiment_score(text),
                "quote": best_quote(text),
                "language": detect_language(text, p.get("language_hints")),
            }
        )
    return {"items": items}


def _theme_labeling(p: dict[str, Any]) -> dict[str, Any]:
    """Aspect + tone + dominant brand, disambiguated by a distinctive term when labels would collide."""
    labels = []
    used: set[str] = set()
    for c in p.get("clusters", []):
        aspect = (c.get("dominant_aspect") or "other").replace("_", " ")
        sent = float(c.get("sentiment", 0.0))
        tone = "complaints" if sent <= -0.2 else "praise" if sent >= 0.2 else "discussion"
        base = f"{aspect.capitalize()} {tone}" if aspect != "other" else f"General {tone}"
        ents = sorted((c.get("entities") or {}).items(), key=lambda kv: (-kv[1], kv[0]))
        n = max(1, int(c.get("n", 0) or 1))
        if ents and ents[0][1] >= 0.5 * n:
            base = f"{base}: {ents[0][0]}"
        label = base
        if label in used:
            ent_words = {e.lower() for e, _ in ents}
            extra = next((t for t in c.get("top_terms", []) if t and t.lower() not in ent_words and " " not in t), "")
            label = f"{base} ({extra})" if extra else f"{base} #{c['cluster_id']}"
        used.add(label)
        labels.append({"cluster_id": c["cluster_id"], "label": label[:80]})
    return {"labels": labels}


def _entity_resolution(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "items": [
            {
                "document_id": d["id"],
                "entities": find_entities(unwrap_untrusted(d.get("text", "")), p.get("aliases", {})),
            }
            for d in p.get("documents", [])
        ]
    }


def _change_classification(p: dict[str, Any]) -> dict[str, Any]:
    items = []
    for c in p.get("changes", []):
        kind, conf = classify_change_rules(unwrap_untrusted(c.get("before", "")), unwrap_untrusted(c.get("after", "")))
        items.append({"index": c["index"], "kind": kind, "confidence": conf})
    return {"items": items}


def _reported_values(p: dict[str, Any]) -> dict[str, Any]:
    items = []
    for d in p.get("documents", []):
        text = unwrap_untrusted(d.get("text", ""))
        v = extract_reported_value(text, p.get("units", []), p.get("keywords", []))
        if v is None:
            items.append({"document_id": d["id"], "value": None, "first_hand": False})
            continue
        items.append(
            {
                "document_id": d["id"],
                "value": v.value,
                "unit": p.get("unit", v.unit),
                "first_hand": True,
                "conditions": extract_conditions(v.sentence),
                "excerpt": v.sentence[:400],
            }
        )
    return {"items": items}


def _claimed_values(p: dict[str, Any]) -> dict[str, Any]:
    items = []
    for d in p.get("documents", []):
        text = unwrap_untrusted(d.get("text", ""))
        v = extract_claimed_value(text, p.get("units", []), p.get("keywords", []))
        if v is not None:
            items.append(
                {
                    "document_id": d["id"],
                    "entity": d.get("entity", ""),
                    "value": v.value,
                    "unit": p.get("unit", v.unit),
                    "excerpt": v.sentence[:400],
                }
            )
    return {"items": items}


def _ask_router(p: dict[str, Any]) -> dict[str, Any]:
    intent, entities, needs = route_intent(p.get("question", ""), p.get("aliases", {}))
    return {"intent": intent, "entities": entities, "needs_research": needs, "search_query": p.get("question", "")}


def _action_items(p: dict[str, Any]) -> dict[str, Any]:
    items = extract_action_items_heuristic(p.get("note", ""))
    for it in items:
        it.pop("flags", None)
    return {"items": items}


HANDLERS: dict[str, Handler] = {
    "voice_tagging": _voice_tagging,
    "theme_labeling": _theme_labeling,
    "entity_resolution": _entity_resolution,
    "change_classification": _change_classification,
    "reported_values": _reported_values,
    "claimed_values": _claimed_values,
    "ask_router": _ask_router,
    "action_items": _action_items,
    "analyst": mock_agents.analyst,
    "critic_entailment": mock_agents.entailment,
    "critic_overreach": mock_agents.overreach,
    "counter_evidence": mock_agents.counter_evidence,
    "writer_prose": mock_agents.writer_prose,
    "ask_claims": mock_agents.ask_claims,
    "ask_answer": mock_agents.ask_answer,
    "workspace_draft": heuristic_draft,
}


class MockClient(LLMClient):
    name = "mock"
    is_mock = True
    cache_namespace = MOCK_VERSION

    def __init__(
        self,
        settings: Settings,
        *,
        cache: LLMCacheStore | None = None,
        cost: CostTracker | None = None,
        responses: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(settings, cache=cache, cost=cost)
        self.responses: dict[str, Any] = dict(responses or {})
        overrides = fixtures_dir() / "mock_llm_overrides.json"
        if overrides.exists() and not responses:
            with contextlib.suppress(OSError, json.JSONDecodeError):
                self.responses.update(json.loads(overrides.read_text(encoding="utf-8")))
        self.calls: list[tuple[str, dict[str, Any]]] = []
        if self.responses:
            # Injected responses must not be shadowed by cached handler output.
            self.cache_namespace = f"{MOCK_VERSION}:{json.dumps(self.responses, sort_keys=True, default=str)[:2000]}"

    def model_for(self, tier: str) -> str:  # type: ignore[override]
        return "mock-fast" if tier == "fast" else "mock-main"

    @staticmethod
    def _usage(model: str, prompt: str, output: str) -> Usage:
        return Usage(
            model=model, input_tokens=max(1, len(prompt) // 4), output_tokens=max(1, len(output) // 4), latency_ms=1
        )

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
        self.calls.append((task, payload))
        if task in self.responses:
            canned = self.responses[task]
            data = canned(payload) if callable(canned) else canned
        elif task in HANDLERS:
            data = HANDLERS[task](payload)
        else:
            raise KeyError(f"MockClient has no handler for task '{task}'")
        obj = schema.model_validate(data)
        return obj, [self._usage(model, system + prompt, obj.model_dump_json())]

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
        self.calls.append((task, payload))
        if task in self.responses:
            canned = self.responses[task]
            result = canned(messages, payload) if callable(canned) else canned
            if isinstance(result, ChatResult):
                return result
            raise TypeError("canned chat responses must return a ChatResult")
        if task != "researcher":
            raise KeyError(f"MockClient has no chat policy for task '{task}'")
        result = mock_agents.researcher_step(messages, payload, force_tool=force_tool)
        result.usages = [
            self._usage(model, system + json.dumps(messages, default=str)[-4000:], json.dumps(result.assistant_content))
        ]
        return result
