"""Ask FieldNote router: classify intent and decide between a single tool and the research path."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from fieldnote.config import WorkspaceConfig
from fieldnote.costs import BudgetExceededError
from fieldnote.heuristics import route_intent
from fieldnote.llm.base import LLMClient, LLMError
from fieldnote.llm.schemas import RouterOutput
from fieldnote.logging_setup import get_logger

log = get_logger(__name__)

INTENTS = ("market_change", "competitor", "customer_voice", "metrics", "opportunities", "coordination", "general")
_FOLLOW_UP = re.compile(r"(?i)\b(it|they|them|their|that|this|those|these|same|what about|and|also|why)\b")


@dataclass
class Route:
    intent: str
    entities: list[str] = field(default_factory=list)
    needs_research: bool = False
    search_query: str = ""
    via: str = "rules"


def route_question(
    cfg: WorkspaceConfig, llm: LLMClient | None, question: str, previous_entities: list[str] | None = None
) -> Route:
    aliases = cfg.entity_aliases()
    route: Route | None = None
    if llm is not None:
        try:
            out = llm.structured(
                task="ask_router",
                system=(
                    "Classify a question for FieldNote, a market-intelligence assistant. Intents: market_change (what changed "
                    "on competitor pages/news), competitor (about one competitor), customer_voice (what customers say), metrics "
                    "(registrations, sales, share), opportunities (findings, risks, what to do), coordination (internal action "
                    f"items), general. Known competitors: {', '.join(cfg.competitor_names())}. needs_research is true for "
                    "multi-part, comparative or 'why' questions."
                ),
                prompt=f"Question: {question}",
                schema=RouterOutput,
                tier="fast",
                payload={"question": question, "aliases": aliases},
            )
            valid = set(cfg.competitor_names())
            route = Route(
                intent=out.intent,
                entities=[e for e in out.entities if e in valid],
                needs_research=out.needs_research,
                search_query=out.search_query or question,
                via="llm",
            )
        except (LLMError, BudgetExceededError) as exc:
            log.info("router fell back to rules: %s", exc)
    if route is None:
        intent, ents, needs = route_intent(question, aliases)
        route = Route(intent=intent, entities=ents, needs_research=needs, search_query=question, via="rules")
    # Short follow-ups ("and their service?") inherit the entities of the previous turn.
    if not route.entities and previous_entities and (len(question.split()) <= 12 or _FOLLOW_UP.search(question)):
        route.entities = list(previous_entities)
        route.search_query = f"{route.search_query} {' '.join(previous_entities)}"
    return route
