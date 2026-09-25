"""Overview: KPIs and top findings."""

from __future__ import annotations

from datetime import timedelta

import streamlit as st

from fieldnote.dashboard.components import DashContext, empty_state, esc, evidence_list, kpi, md, run_caption, tag
from fieldnote.db import repo
from fieldnote.reports.common import evidence_items, verification_stats
from fieldnote.scoring.opportunities import rank


def render(ctx: DashContext) -> None:
    cfg = ctx.cfg
    run_caption(ctx)
    with ctx.db.session() as s:
        total_docs = repo.count_documents(s, cfg.workspace)
        recent_docs = len(repo.documents_between(s, cfg.workspace, ctx.now - timedelta(days=7), ctx.now))
        changes7 = len(repo.changes_between(s, cfg.workspace, ctx.now - timedelta(days=7), ctx.now))
        ver = verification_stats(s, cfg.workspace, [ctx.run_id]) if ctx.run_id else {"verified_pct": 0.0, "checked": 0}
        open_actions = len(repo.open_action_items(s, cfg.workspace))
        rows = repo.findings(s, cfg.workspace, statuses=("active", "demoted"), run_id=ctx.run_id) if ctx.run_id else []
        ranked = rank(
            [
                {
                    "id": f.id,
                    "title": f.title,
                    "scores": f.scores or {},
                    "evidence_count": f.evidence_count,
                    "last_seen": f.last_seen,
                    "row": f,
                }
                for f in rows
            ],
            cfg.scoring.weights,
        )[: cfg.scoring.top_n_in_brief]
        top = [(x, evidence_items(s, list(x["row"].evidence_claim_ids or []))) for x in ranked]
    cols = st.columns(5)
    kpi(cols[0], "Documents collected", f"{total_docs:,}", f"{recent_docs:,} in the last 7 days")
    kpi(cols[1], "Page changes (7 days)", str(changes7), "competitor pages")
    kpi(cols[2], "Verified claims", f"{ver['verified_pct']:.0f}%", f"of {ver['checked']} checked in the latest run")
    kpi(cols[3], "Open actions", str(open_actions), "from meeting notes")
    status = ctx.run.status if ctx.run else "no runs"
    kpi(cols[4], "Last run", status, f"${ctx.run.cost_usd:.2f} LLM cost" if ctx.run else "run `fieldnote run`")
    st.subheader("Top findings")
    if not top:
        empty_state("No findings yet for this workspace.", "Run `fieldnote run` (or `fieldnote demo` for fixtures).")
        return
    for item, ev in top:
        f = item["row"]
        with st.container(border=True):
            st.markdown(
                f"#### {item['rank']}. {md(f.title)}\n"
                + tag(f.category)
                + tag(f"score {item['total']:.2f}")
                + (tag("thin evidence") if f.status == "demoted" else "")
                + (tag("disputed") if (f.checks or {}).get("disputed") else ""),
                unsafe_allow_html=True,
            )
            st.markdown(f"**What happened** {md(f.what_happened)}")
            st.markdown(
                f"**Why it matters** <span class='fn-muted'>(analysis)</span> {esc(f.why_it_matters)}",
                unsafe_allow_html=True,
            )
            st.markdown(f"**Recommended action** {md(f.recommended_action)}")
            with st.expander("How to execute and evidence"):
                st.markdown(md(f.how_to_execute))
                evidence_list(ev)
