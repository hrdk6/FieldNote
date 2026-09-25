"""Analyst agent: turns verified fact/estimate claims into candidate findings (opportunities / risks)."""

from __future__ import annotations

from typing import Any

from fieldnote.agents.context import AgentContext
from fieldnote.agents.critic import metric_entity_shares
from fieldnote.llm.schemas import AnalystOutput
from fieldnote.logging_setup import get_logger

log = get_logger(__name__)


def analyst_system(ctx: AgentContext) -> str:
    cfg = ctx.cfg
    focal = (
        f"The focal company is {cfg.focal_company}."
        if cfg.focal_company
        else "There is no focal company: write from a market-level viewpoint."
    )
    return f"""You are the Analyst in FieldNote for {cfg.sector} in {cfg.region}.
Perspective: {cfg.perspective}. {focal}
Aspects tracked: {", ".join(cfg.aspects)}.

You receive ONLY claims that already passed verification. Produce decision-ready findings, each either an
`opportunity` or a `risk`, with:
- what_happened: restate the verified evidence (no new numbers or names);
- why_it_matters: from the perspective above;
- evidence_claim_ids: ids of the verified claims it rests on (at least one);
- analysis_claims: your inferences, each listing the claim ids it depends on; no new facts or numbers;
- recommended_action and how_to_execute (owners as roles, sequence, timeframe);
- ratings 1-5 with a one-sentence rationale each: market_size (how much of the market it touches),
  competitor_gap (how exposed the gap is), effort (1 = very low effort to act, 5 = very high).
Group related claims into one finding. Do not invent claims. Prefer 3-8 findings. Estimates must stay
labelled as estimates; never present a low-confidence comparison as fact."""


def claims_payload(claims: list[Any], docs_types: dict[int, str]) -> list[dict[str, Any]]:
    out = []
    for c in claims:
        out.append(
            {
                "id": c.id,
                "type": c.type,
                "text": c.text,
                "entities": list(c.entities or []),
                "tags": list(c.tags or []),
                "aspect": c.aspect,
                "source_types": sorted({docs_types.get(int(i), "other") for i in (c.source_document_ids or [])}),
            }
        )
    return out


def run_analyst(ctx: AgentContext, verified: list[Any], docs_types: dict[int, str]) -> AnalystOutput:
    if not verified:
        return AnalystOutput(findings=[])
    shares, label = metric_entity_shares(ctx)
    payload_claims = claims_payload(verified, docs_types)
    lines = [
        f"[claim {c['id']}] ({c['type']}; tags: {', '.join(c['tags'])}; entities: {', '.join(c['entities']) or '-'}; "
        f"sources: {', '.join(c['source_types'])}) {c['text']}"
        for c in payload_claims
    ]
    share_line = (
        ", ".join(f"{k} {v:.1f}%" for k, v in sorted(shares.items(), key=lambda kv: -kv[1]))
        if shares
        else "not available"
    )
    prompt = f"Tracked-entity shares of {label} (computed in code): {share_line}\n\nVerified claims:\n" + "\n".join(
        lines
    )
    with ctx.tracer.step("analyst", "draft_findings", input_summary=f"{len(verified)} verified claims") as tr:
        out = ctx.llm.structured(
            task="analyst",
            system=analyst_system(ctx),
            prompt=prompt,
            schema=AnalystOutput,
            tier="main",
            payload={
                "claims": payload_claims,
                "perspective": ctx.cfg.perspective,
                "entity_shares": shares,
                "metric_label": label,
                "aspects": ctx.cfg.aspects,
            },
            max_tokens=12000,
        )
        valid_ids = {c.id for c in verified}
        kept = []
        for f in out.findings:
            f.evidence_claim_ids = [i for i in f.evidence_claim_ids if i in valid_ids]
            for a in f.analysis_claims:
                a.depends_on = [i for i in a.depends_on if i in valid_ids]
            f.analysis_claims = [a for a in f.analysis_claims if a.depends_on]
            if f.evidence_claim_ids:
                kept.append(f)
        dropped = len(out.findings) - len(kept)
        out.findings = kept
        tr.output_summary = f"{len(kept)} findings drafted ({dropped} dropped for invalid evidence ids)"
    return out
