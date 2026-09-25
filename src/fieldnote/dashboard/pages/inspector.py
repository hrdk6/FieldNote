"""Run Inspector: stages, agent traces, tool calls, tokens, cost, latency, failures, and critic decisions."""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from fieldnote.dashboard.components import DashContext, empty_state, md
from fieldnote.db import repo


def render(ctx: DashContext) -> None:
    with ctx.db.session() as s:
        runs = [
            {
                "id": r.id,
                "date": r.run_date,
                "mode": r.mode,
                "kind": r.kind,
                "status": r.status,
                "cost": r.cost_usd,
                "tokens_in": r.tokens_in,
                "tokens_out": r.tokens_out,
                "stats": r.stats or {},
                "started": r.started_at,
                "finished": r.finished_at,
            }
            for r in repo.list_runs(s, ctx.cfg.workspace)
        ]
    if not runs:
        empty_state("No runs yet.", "Run `fieldnote run` or `fieldnote demo`.")
        return
    sel = st.selectbox(
        "Run",
        [r["id"] for r in runs],
        format_func=lambda i: next(
            f"#{r['id']} · {r['date']} · {r['kind']} · {r['mode']} · {r['status']}" for r in runs if r["id"] == i
        ),
    )
    run = next(r for r in runs if r["id"] == sel)
    stats = run["stats"]
    dur = (run["finished"] - run["started"]).total_seconds() if run["finished"] else None
    c = st.columns(4)
    c[0].metric("Status", run["status"])
    c[1].metric("LLM cost", f"${run['cost']:.4f}")
    c[2].metric("Tokens in / out", f"{run['tokens_in']:,} / {run['tokens_out']:,}")
    c[3].metric("Duration", f"{dur:.1f}s" if dur is not None else "-")
    st.subheader("Pipeline stages")
    st.dataframe(pd.DataFrame(stats.get("stages", [])), hide_index=True, width="stretch")
    if stats.get("collectors"):
        st.subheader("Collectors")
        st.dataframe(
            pd.DataFrame([{"collector": k, **v} for k, v in stats["collectors"].items()]),
            hide_index=True,
            width="stretch",
        )
    agents = stats.get("agents", {})
    if agents.get("stages"):
        st.subheader("Agent state machine")
        st.dataframe(pd.DataFrame(agents["stages"]), hide_index=True, width="stretch")
    if agents.get("disagreements"):
        st.subheader("Source disagreements recorded")
        for d in agents["disagreements"]:
            st.markdown(md(f"- {d['description']} (claims {d.get('claim_ids')})"))
    with ctx.db.session() as s:
        traces = repo.traces_for_run(s, ctx.cfg.workspace, sel)
        trows = [
            {
                "agent": t.agent,
                "step": t.step,
                "status": t.status,
                "tool calls": len(t.tool_calls or []),
                "tokens in": t.tokens_in,
                "tokens out": t.tokens_out,
                "cost": round(t.cost, 5),
                "latency ms": t.latency_ms,
                "error": t.error,
                "output": t.output_summary[:200],
                "_calls": t.tool_calls,
            }
            for t in traces
        ]
        claims = [
            {
                "id": c.id,
                "agent": c.agent,
                "type": c.type,
                "status": c.status,
                "text": c.text[:160],
                "critic": c.critic_notes[:160],
                "stage 2": (c.checks or {}).get("stage2", ""),
            }
            for c in repo.claims_for_run(s, ctx.cfg.workspace, sel)
        ]
    st.subheader("Agent traces")
    if trows:
        st.dataframe(
            pd.DataFrame([{k: v for k, v in r.items() if k != "_calls"} for r in trows]),
            hide_index=True,
            width="stretch",
        )
        pick = st.selectbox(
            "Tool calls for step",
            list(range(len(trows))),
            format_func=lambda i: f"{trows[i]['agent']} · {trows[i]['step']}",
        )
        st.code(json.dumps(trows[pick]["_calls"], indent=1, default=str)[:20000], language="json")
    else:
        st.caption("No agent traces for this run (baseline runs only collect and process).")
    st.subheader("Claims and critic decisions")
    if claims:
        df = pd.DataFrame(claims)
        st.dataframe(df, hide_index=True, width="stretch")
        st.caption(
            f"{(df['status'] == 'verified').sum()} verified · {(df['status'] == 'rejected').sum()} rejected · "
            f"{(df['status'] == 'unverified').sum()} withheld. Only verified claims can appear in outputs."
        )
