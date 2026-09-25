"""Evals: latest report and history."""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from fieldnote.config import out_dir
from fieldnote.dashboard.components import DashContext, empty_state, md
from fieldnote.dashboard.theme import style_figure
from fieldnote.db import repo


def render(ctx: DashContext) -> None:
    with ctx.db.session() as s:
        hist = [
            {"id": e.id, "created": e.created_at, "mode": e.mode, "passed": e.passed, "metrics": e.metrics or {}}
            for e in repo.eval_history(s, ctx.cfg.workspace, limit=50)
        ]
    if not hist:
        empty_state("No eval results yet.", "Run `fieldnote eval` (or `fieldnote demo`).")
        return
    latest = hist[0]
    m = latest["metrics"]
    st.caption(
        f"Latest eval #{latest['id']} · {latest['created']:%Y-%m-%d %H:%M} · LLM path: {latest['mode']} · "
        f"{'PASS' if latest['passed'] else 'FAIL'}"
    )
    cols = st.columns(5)
    if "citation_validity" in m:
        cols[0].metric("Citation validity", f"{m['citation_validity']['rate']:.0%}")
        cols[1].metric(
            "Critic catch rate",
            f"{m['adversarial']['rate']:.0%}",
            help="Share of programmatically corrupted claims the critic rejects",
        )
        cols[2].metric("Number consistency", f"{m['number_consistency']['rate']:.0%}")
        cols[3].metric("Ranking deterministic", "yes" if m["ranking"]["deterministic"] else "no")
    ex = m.get("extraction", {})
    if ex:
        cols[4].metric("Extraction P / R", f"{ex['precision']:.0%} / {ex['recall']:.0%}")
    checks = m.get("checks", {})
    if checks:
        st.dataframe(
            pd.DataFrame([{"check": k, "pass": v} for k, v in checks.items()]), hide_index=True, width="stretch"
        )
    if "adversarial" in m:
        st.subheader("Adversarial corruptions caught, by type")
        bk = m["adversarial"]["by_kind"]
        df = pd.DataFrame(
            [{"type": k, "caught": v["caught"], "missed": v["total"] - v["caught"]} for k, v in bk.items()]
        )
        fig = px.bar(
            df.melt(id_vars="type", var_name="outcome", value_name="claims"),
            x="claims",
            y="type",
            color="outcome",
            orientation="h",
            color_discrete_map={"caught": "#0ca30c", "missed": "#d03b3b"},
        )
        st.plotly_chart(style_figure(fig, height=260), width="stretch")
    if len(hist) > 1:
        st.subheader("History")
        rows = []
        for h in reversed(hist):
            hm = h["metrics"]
            if "citation_validity" not in hm:
                continue
            rows += [
                {"created": h["created"], "metric": "citation validity", "value": hm["citation_validity"]["rate"]},
                {"created": h["created"], "metric": "critic catch rate", "value": hm["adversarial"]["rate"]},
                {"created": h["created"], "metric": "number consistency", "value": hm["number_consistency"]["rate"]},
            ]
        if rows:
            fig = px.line(pd.DataFrame(rows), x="created", y="value", color="metric", markers=True)
            fig.update_yaxes(range=[0, 1.05], tickformat=".0%")
            st.plotly_chart(style_figure(fig, height=280), width="stretch")
    report = out_dir() / "eval_report.md"
    if report.exists():
        with st.expander("Full eval report (out/eval_report.md)"):
            st.markdown(md(report.read_text(encoding="utf-8")))
