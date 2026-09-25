"""Market & competitors: change feed with side-by-side diffs and a per-competitor timeline."""

from __future__ import annotations

import difflib
from datetime import timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

from fieldnote.dashboard.components import DashContext, empty_state, esc, md
from fieldnote.dashboard.theme import entity_colors, style_figure
from fieldnote.db import repo

KINDS = ["price", "feature", "launch", "policy", "dealer", "other"]


def _diff_html(before: str, after: str) -> tuple[str, str]:
    sm = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    b_out, a_out = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        b_seg, a_seg = esc(before[i1:i2]), esc(after[j1:j2])
        if op == "equal":
            b_out.append(b_seg)
            a_out.append(a_seg)
        else:
            if b_seg:
                b_out.append(f"<del style='background:rgba(208,59,59,.18);text-decoration:line-through'>{b_seg}</del>")
            if a_seg:
                a_out.append(f"<ins style='background:rgba(12,163,12,.18);text-decoration:none'>{a_seg}</ins>")
    return "".join(b_out) or "<i>(not present)</i>", "".join(a_out) or "<i>(removed)</i>"


def render(ctx: DashContext) -> None:
    cfg = ctx.cfg
    c1, c2, c3 = st.columns([2, 2, 1])
    kinds = c1.multiselect("Change type", KINDS, default=KINDS)
    comps = c2.multiselect("Competitor", cfg.competitor_names(), default=cfg.competitor_names())
    days = c3.selectbox("Window", [7, 14, 30, 90, 365], index=2, format_func=lambda d: f"last {d} days")
    with ctx.db.session() as s:
        rows = repo.changes_between(s, cfg.workspace, ctx.now - timedelta(days=days), ctx.now)
        data = [
            {
                "id": r.id,
                "competitor": r.competitor,
                "kind": r.kind,
                "significance": r.significance,
                "summary": r.summary,
                "before": r.before,
                "after": r.after,
                "url": r.url,
                "detected_at": r.detected_at,
            }
            for r in rows
        ]
    data = [d for d in data if d["kind"] in kinds and d["competitor"] in comps]
    if not data:
        empty_state(
            "No competitor page changes in this window.",
            "Pages are diffed against their previous snapshot on every run.",
        )
        return
    df = pd.DataFrame(data)
    colors = entity_colors(cfg.competitor_names())
    fig = px.scatter(
        df,
        x="detected_at",
        y="competitor",
        color="competitor",
        symbol="kind",
        size="significance",
        size_max=16,
        hover_data={"summary": True, "kind": True, "significance": ":.2f", "detected_at": "|%Y-%m-%d"},
        color_discrete_map=colors,
        category_orders={"competitor": cfg.competitor_names()},
    )
    fig.update_layout(title="Change timeline by competitor", xaxis_title=None, yaxis_title=None)
    st.plotly_chart(style_figure(fig, height=300), width="stretch")
    st.subheader(f"Change feed ({len(data)})")
    for d in sorted(data, key=lambda x: (-x["significance"], x["detected_at"])):
        with st.expander(
            f"{d['competitor']} · {d['kind']} · significance {d['significance']:.2f} · {d['detected_at']:%Y-%m-%d}"
        ):
            st.markdown(f"{md(d['summary'])}  \n[Open page]({d['url']})")
            b, a = _diff_html(d["before"], d["after"])
            left, right = st.columns(2)
            left.markdown("**Before**")
            left.markdown(f"<div style='white-space:pre-wrap;font-size:.9rem'>{b}</div>", unsafe_allow_html=True)
            right.markdown("**After**")
            right.markdown(f"<div style='white-space:pre-wrap;font-size:.9rem'>{a}</div>", unsafe_allow_html=True)
