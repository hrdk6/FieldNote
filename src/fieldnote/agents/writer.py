"""Writer agent: the LLM writes only prose fields; structure is rendered deterministically.

The writer never sees raw documents: its input is the finding plus the text of its *verified* claims.
Post-check: every number and competitor name in each prose field must exist in a verified claim of
that finding (timeframes such as "within 2 weeks" are allowed in action fields). A failing field is
regenerated once with feedback, then replaced by a plain templated sentence built only from verified
claim text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fieldnote.agents.context import AgentContext
from fieldnote.config import WorkspaceConfig
from fieldnote.costs import BudgetExceededError
from fieldnote.llm.base import LLMError
from fieldnote.llm.schemas import ProseOutput
from fieldnote.logging_setup import get_logger
from fieldnote.textutil import extract_numbers, find_entities, number_supported, truncate_words

log = get_logger(__name__)

PROSE_FIELDS = ("what_happened", "why_it_matters", "recommended_action", "how_to_execute")
ACTION_FIELDS = {"recommended_action", "how_to_execute"}


@dataclass
class CheckResult:
    ok: bool
    bad_numbers: list[str] = field(default_factory=list)
    bad_entities: list[str] = field(default_factory=list)


def post_check(
    text: str, allowed_texts: list[str], cfg: WorkspaceConfig, allow_timeframes: bool = False
) -> CheckResult:
    aliases = cfg.entity_aliases()
    allowed_blob = " ".join(allowed_texts)
    allowed_nums = extract_numbers(allowed_blob, skip_dates=False)
    bad_nums = [
        n.raw for n in extract_numbers(text, skip_timeframes=allow_timeframes) if not number_supported(n, allowed_nums)
    ]
    allowed_ents = set(find_entities(allowed_blob, aliases))
    bad_ents = sorted(set(find_entities(text, aliases)) - allowed_ents)
    return CheckResult(ok=not bad_nums and not bad_ents, bad_numbers=bad_nums, bad_entities=bad_ents)


def fallback_text(field_name: str, finding: dict[str, Any], claims: list[dict[str, Any]], word_budget: int) -> str:
    facts = [c["text"] for c in claims if c["type"] in ("fact", "estimate")]
    analysis = [c["text"] for c in claims if c["type"] == "analysis"]
    ents = finding.get("entities") or []
    if field_name == "what_happened":
        return truncate_words(facts[0], word_budget, "…") if facts else "See the linked evidence."
    if field_name == "why_it_matters":
        return (
            analysis[0]
            if analysis
            else "The supporting analysis did not pass verification; review the evidence directly."
        )
    if field_name == "recommended_action":
        who = f" on {', '.join(ents)}" if ents else ""
        return f"Review the verified evidence{who} and agree an owner and a response."
    return "Assign an owner, confirm the facts against the linked sources, and agree next steps at the next planning review."


def writer_system(cfg: WorkspaceConfig) -> str:
    return f"""You are the Writer in FieldNote. Write concise, neutral, decision-ready prose for one finding in a
daily brief about {cfg.sector} in {cfg.region}, from the perspective of a {cfg.perspective}.
Use ONLY the verified claims provided. Every number and brand you mention must appear in those claims.
Do not add new facts, figures or names. Keep estimates described as estimates. Plain sentences, no
markdown. how_to_execute: owners as roles, sequence, timeframe."""


def _prompt(finding: dict[str, Any], claims: list[dict[str, Any]], budget: int, feedback: str = "") -> str:
    lines = [
        f"Finding ({finding['category']}): {finding['title']}",
        f"Analyst notes - what happened: {finding.get('what_happened', '')}",
        f"Why it matters: {finding.get('why_it_matters', '')}",
        f"Recommended action: {finding.get('recommended_action', '')}",
        f"How to execute: {finding.get('how_to_execute', '')}",
        "Verified claims:",
        *[f"- [{c['type']}] {c['text']}" for c in claims],
        f"Word budget for what_happened + why_it_matters + recommended_action: about {budget} words.",
    ]
    if feedback:
        lines.append(f"Your previous draft failed verification: {feedback}. Remove anything not in the claims.")
    return "\n".join(lines)


def write_finding_prose(
    ctx: AgentContext, finding: dict[str, Any], claims: list[dict[str, Any]], word_budget: int
) -> dict[str, Any]:
    """Return prose fields plus bookkeeping about regenerations and fallbacks."""
    allowed = [c["text"] for c in claims]
    result: dict[str, Any] = {"regenerated": [], "fallback": []}
    draft: ProseOutput | None = None
    feedback = ""
    for attempt in range(2):
        try:
            draft = ctx.llm.structured(
                task="writer_prose",
                system=writer_system(ctx.cfg),
                prompt=_prompt(finding, claims, word_budget, feedback),
                schema=ProseOutput,
                tier="main",
                payload={
                    "finding": finding,
                    "claims": claims,
                    "analysis": [c["text"] for c in claims if c["type"] == "analysis"],
                    "word_budget": word_budget,
                    "attempt": attempt,
                },
            )
        except BudgetExceededError:
            raise
        except LLMError as exc:
            log.info("writer LLM call failed; using templates: %s", exc)
            draft = None
            break
        failures = []
        for f in PROSE_FIELDS:
            chk = post_check(getattr(draft, f), allowed, ctx.cfg, allow_timeframes=f in ACTION_FIELDS)
            if not chk.ok:
                failures.append(f"{f}: unsupported {chk.bad_numbers + chk.bad_entities}")
        if not failures:
            break
        feedback = "; ".join(failures)
        if attempt == 0:
            result["regenerated"] = [x.split(":")[0] for x in failures]
    for f in PROSE_FIELDS:
        text = getattr(draft, f) if draft is not None else ""
        chk = post_check(text, allowed, ctx.cfg, allow_timeframes=f in ACTION_FIELDS) if text else CheckResult(ok=False)
        if not text or not chk.ok:
            text = fallback_text(f, finding, claims, max(20, word_budget // 2))
            result["fallback"].append(f)
        result[f] = text
    result["headline"] = (draft.headline if draft and draft.headline else finding["title"])[:160]
    hchk = post_check(result["headline"], [*allowed, finding["title"]], ctx.cfg)
    if not hchk.ok:
        result["headline"] = finding["title"]
    return result
