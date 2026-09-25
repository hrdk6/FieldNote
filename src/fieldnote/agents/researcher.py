"""Researcher agent: a bounded tool loop that gathers evidence and emits sourced fact/estimate claims."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from fieldnote.agents.context import AgentContext, RunContextSummary
from fieldnote.agents.tools import tool_specs
from fieldnote.llm.base import LLMError, ToolSpec, inline_refs
from fieldnote.llm.schemas import ResearchOutput
from fieldnote.logging_setup import get_logger
from fieldnote.textutil import UNTRUSTED_NOTICE

log = get_logger(__name__)

SUBMIT_TOOL = "submit_claims"


def researcher_system(ctx: AgentContext) -> str:
    cfg = ctx.cfg
    focal = (
        f"The focal company is {cfg.focal_company}."
        if cfg.focal_company
        else "There is no focal company; take a market-level view."
    )
    return f"""You are the Researcher in FieldNote, an AI chief-of-staff tracking {cfg.sector} in {cfg.region}.
Perspective: {cfg.perspective}. {focal}

Your job: use the read-only tools to gather evidence about what changed, then call `{SUBMIT_TOOL}` once with
sourced claims. Rules:
- A `fact` must be directly supported by a document: give >=1 source_document_ids and an `excerpt` copied
  VERBATIM from the first source's content (max ~40 words). Every number and brand in the claim text must
  appear in that excerpt.
- An `estimate` is a computed or approximate figure. Start its text with "Estimate:", and include a
  `derivation` (method, op, operands, result) using only numbers returned by the tools. For
  claim-vs-reported or theme figures, use op "reference" with the reference fields given by the tool.
  If a claim-vs-reported row is flagged low_confidence, say "low confidence" and never state the
  comparison as fact.
- Prefer primary sources (company pages, filings, official releases, FieldNote change records and dataset
  summaries) over commentary.
- If sources disagree, keep both claims and record the disagreement; do not resolve it silently.
- Never name or describe individual people; refer to "a customer post" instead.
- Tag each claim (e.g. change, price, launch, policy, dealer, feature, metric, metric_up, metric_down,
  claim_vs_reported, gap, voice, voice_negative, emerging, news) and list competitor entities.
- You have at most {cfg.limits.researcher_max_steps} tool-use turns. Submit before you run out.
{UNTRUSTED_NOTICE}"""


def submit_tool() -> ToolSpec:
    return ToolSpec(
        name=SUBMIT_TOOL,
        description="Submit the final list of sourced claims and any disagreements between sources.",
        input_schema=inline_refs(ResearchOutput.model_json_schema()),
    )


def run_researcher(
    ctx: AgentContext,
    summary: RunContextSummary,
    *,
    focus: str | None = None,
    max_claims: int | None = None,
) -> ResearchOutput:
    cfg = ctx.cfg
    max_steps = cfg.limits.researcher_max_steps
    max_claims = max_claims or cfg.limits.max_claims_per_run
    tools = [*tool_specs(), submit_tool()]
    task_line = (
        f"Answer this question using only stored evidence: {focus}"
        if focus
        else "Research what changed in this run and why it could matter."
    )
    prompt = (
        f"{task_line}\n\nRun context (computed by FieldNote):\n{summary.to_prompt()}\n\n"
        f"Suggested searches: {', '.join(summary.queries)}\nSince: {ctx.since.isoformat(timespec='seconds')}\n"
        f"Return at most {max_claims} claims."
    )
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    payload = {
        "queries": summary.queries if not focus else [focus, *summary.queries[:2]],
        "since": ctx.since.isoformat(timespec="seconds"),
        "max_claims": max_claims,
        "aliases": cfg.entity_aliases(),
        "aspect_terms": cfg.aspect_term_map(),
        "focus": focus,
        "entities": cfg.competitor_names(),
        "max_documents": 28,
    }
    system = researcher_system(ctx)
    for step in range(max_steps):
        force = SUBMIT_TOOL if step == max_steps - 1 else None
        with ctx.tracer.step("researcher", f"step_{step + 1}", input_summary=f"{len(messages)} messages") as tr:
            res = ctx.llm.chat_with_tools(
                task="researcher",
                system=system,
                messages=messages,
                tools=tools,
                force_tool=force,
                payload=payload,
                max_tokens=8000,
            )
            messages.append(
                {
                    "role": "assistant",
                    "content": res.assistant_content or [{"type": "text", "text": res.text or "(no text)"}],
                }
            )
            if not res.tool_calls:
                messages.append({"role": "user", "content": f"Use the tools if needed, then call `{SUBMIT_TOOL}`."})
                tr.output_summary = "no tool calls; nudged"
                continue
            # Every tool_use must get a tool_result in the next user turn, including a failed submission.
            results = []
            for call in res.tool_calls:
                if call.name == SUBMIT_TOOL:
                    try:
                        out = ResearchOutput.model_validate(call.input)
                    except ValidationError as exc:
                        tr.status = "retry"
                        tr.tool_calls.append({"tool": SUBMIT_TOOL, "error": "validation failed"})
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": call.id,
                                "is_error": True,
                                "content": f"Validation failed: {str(exc)[:1500]}. Fix and call {SUBMIT_TOOL} again.",
                            }
                        )
                        continue
                    tr.tool_calls.append({"tool": SUBMIT_TOOL, "claims": len(out.claims)})
                    tr.output_summary = f"{len(out.claims)} claims, {len(out.disagreements)} disagreements"
                    out.claims = out.claims[:max_claims]
                    return out
                out_obj = ctx.tools.dispatch(call.name, call.input)
                tr.tool_calls.append({"tool": call.name, "args": call.input, "error": out_obj.get("error")})
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": json.dumps(out_obj, default=str, ensure_ascii=False)[:60000],
                    }
                )
            messages.append({"role": "user", "content": results})
            tr.output_summary = tr.output_summary or f"{len(res.tool_calls)} tool calls"
    log.warning("researcher finished without submitting claims")
    raise LLMError("researcher did not submit claims within the step limit")
