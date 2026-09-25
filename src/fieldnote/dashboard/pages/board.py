"""Opportunity Board: ranked findings with score breakdowns; weight sliders re-rank live (no LLM calls)."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from fieldnote.dashboard.components import DashContext, empty_state, evidence_list, md, tag
from fieldnote.db import repo
from fieldnote.reports.common import evidence_items
from fieldnote.scoring.opportunities import CRITERIA, rank

LABELS = {
    "market_size": "Market size",
    "competitor_gap": "Competitor gap",
    "evidence_strength": "Evidence strength",
    "effort": "Effort (inverse)",
}


def render(ctx: DashContext) -> None:
    cfg = ctx.cfg
    statuses = st.segmented_control(
        "Show", ["active", "demoted", "stale"], selection_mode="multi", default=["active", "demoted"]
    )
    with ctx.db.session() as s:
        rows = repo.findings(s, cfg.workspace, statuses=tuple(statuses or ["active"]))
        items = [
            {
                "id": f.id,
                "title": f.title,
                "category": f.category,
                "status": f.status,
                "scores": {k: float((f.scores or {}).get(k, 0.0)) for k in CRITERIA},
                "evidence_count": f.evidence_count,
                "last_seen": f.last_seen,
                "first_seen": f.first_seen,
                "raw": f.raw_ratings or {},
                "checks": f.checks or {},
                "what": f.what_happened,
                "why": f.why_it_matters,
                "action": f.recommended_action,
                "how": f.how_to_execute,
                "evidence": evidence_items(s, list(f.evidence_claim_ids or [])),
            }
            for f in rows
        ]
    if not items:
        empty_state("The board is empty.", "Findings appear after a run whose claims pass the critic.")
        return
    st.markdown("**Weights** (move the sliders to re-rank; nothing is recomputed by the LLM)")
    cols = st.columns(4)
    weights = {
        c: cols[i].slider(LABELS[c], 0.0, 1.0, float(cfg.scoring.weights[c]), 0.05, key=f"w_{c}")
        for i, c in enumerate(CRITERIA)
    }
    if sum(weights.values()) == 0:
        st.warning("All weights are zero; using the workspace defaults.")
        weights = dict(cfg.scoring.weights)
    ranked = rank(items, weights)
    table = pd.DataFrame(
        [
            {
                "rank": x["rank"],
                "finding": x["title"],
                "type": x["category"],
                "status": x["status"],
                **{LABELS[c]: round(x["scores"][c], 2) for c in CRITERIA},
                "total": round(x["total"], 3),
                "evidence": x["evidence_count"],
                "last seen": x["last_seen"].strftime("%Y-%m-%d"),
            }
            for x in ranked
        ]
    )
    st.dataframe(
        table,
        hide_index=True,
        width="stretch",
        column_config={
            LABELS[c]: st.column_config.ProgressColumn(LABELS[c], min_value=0, max_value=1, format="%.2f")
            for c in CRITERIA
        },
    )
    st.caption(
        "Evidence strength is computed from distinct verified sources, source-type diversity, recency and primary sources. "
        "Ties break by evidence count, then recency."
    )
    for x in ranked:
        with st.expander(md(f"{x['rank']}. {x['title']}  ·  {x['total']:.3f}")):
            st.markdown(
                tag(x["category"]) + tag(x["status"]) + (tag("disputed") if x["checks"].get("disputed") else ""),
                unsafe_allow_html=True,
            )
            st.markdown(
                md(
                    f"**What happened** {x['what']}\n\n**Why it matters** (analysis) {x['why']}\n\n"
                    f"**Recommended action** {x['action']}\n\n**How to execute** {x['how']}"
                )
            )
            rr = x["raw"]
            if rr:
                st.markdown("**Analyst ratings (1-5)**")
                for k in ("market_size", "competitor_gap", "effort"):
                    if k in rr:
                        st.markdown(md(f"- {LABELS[k]}: {rr[k].get('value')} - {rr[k].get('rationale', '')}"))
            eb = (x["checks"] or {}).get("counter_evidence")
            if eb:
                st.warning(md("Counter-evidence found: " + "; ".join(j.get("note", "") for j in eb)))
            st.markdown("**Evidence**")
            evidence_list(x["evidence"])
