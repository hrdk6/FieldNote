"""Metrics: trends and shares from configured connectors (all arithmetic done in code)."""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from fieldnote.dashboard.components import DashContext, empty_state, md
from fieldnote.dashboard.theme import entity_colors, style_figure
from fieldnote.db import repo
from fieldnote.processing.metrics import metric_summaries


def render(ctx: DashContext) -> None:
    cfg = ctx.cfg
    if not cfg.connectors:
        empty_state(
            "No metrics connector is configured for this workspace.",
            "Add a csv_file or http_json connector to the workspace YAML (see docs/adding-a-workspace.md).",
        )
        return
    with ctx.db.session() as s:
        summaries = metric_summaries(s, cfg)
        points = repo.metric_points(s, cfg.workspace)
        pts = [
            {"connector": p.connector, "entity": p.entity, "region": p.region, "period": p.period, "value": p.value}
            for p in points
        ]
    if not summaries:
        empty_state(
            "A connector is configured but no data has been loaded yet.",
            "Place the CSV at the configured path (downloaded manually, respecting the source's terms) and run the pipeline.",
        )
        return
    summ = (
        summaries[0]
        if len(summaries) == 1
        else next(x for x in summaries if x.name == st.selectbox("Connector", [x.name for x in summaries]))
    )
    df = pd.DataFrame([p for p in pts if p["connector"] == summ.name])
    colors = entity_colors(cfg.competitor_names() + sorted(set(df["entity"]) - set(cfg.competitor_names())))
    st.caption(
        md(
            f"Source: {summ.source_url or 'user-supplied file'} · unit: {summ.unit} · latest period {summ.latest_period}"
        )
    )
    regions = sorted(df["region"].unique())
    sel = st.multiselect("Regions", regions, default=regions)
    sub = df[df["region"].isin(sel)]
    trend = sub.groupby(["period", "entity"], as_index=False)["value"].sum()
    fig = px.line(
        trend,
        x="period",
        y="value",
        color="entity",
        markers=True,
        color_discrete_map=colors,
        labels={"value": summ.unit_short, "period": ""},
    )
    fig.update_layout(title=f"{summ.label.capitalize()} by entity")
    st.plotly_chart(style_figure(fig), width="stretch")
    c1, c2 = st.columns(2)
    latest = sub[sub["period"] == summ.latest_period].groupby("entity", as_index=False)["value"].sum()
    latest["share"] = latest["value"] / latest["value"].sum() * 100 if len(latest) else 0
    latest = latest.sort_values("share", ascending=True)
    fig2 = px.bar(
        latest,
        x="share",
        y="entity",
        orientation="h",
        color="entity",
        color_discrete_map=colors,
        labels={"share": "share (%)", "entity": ""},
        text=latest["share"].map(lambda v: f"{v:.1f}%"),
    )
    fig2.update_layout(title=f"Share of {summ.label}, {summ.latest_period}")
    fig2.update_traces(textposition="outside", cliponaxis=False, width=0.6)
    c1.plotly_chart(style_figure(fig2, legend=False), width="stretch")
    by_region = (
        sub[sub["period"] == summ.latest_period].groupby("region", as_index=False)["value"].sum().sort_values("value")
    )
    fig3 = px.bar(by_region, x="value", y="region", orientation="h", labels={"value": summ.unit_short, "region": ""})
    fig3.update_layout(title=f"{summ.label.capitalize()} by region, {summ.latest_period}")
    fig3.update_traces(width=0.6)
    c2.plotly_chart(style_figure(fig3, legend=False), width="stretch")
    st.subheader("Top movers (month over month)")
    movers = [
        {
            "entity": e.entity,
            "latest": e.latest,
            "previous": e.previous,
            "change %": round(e.pct_change or 0.0, 1),
            "share %": round(e.share or 0.0, 1),
        }
        for e in summ.entities
        if e.entity in summ.movers
    ]
    st.dataframe(
        pd.DataFrame(movers).sort_values("change %", key=abs, ascending=False), hide_index=True, width="stretch"
    )
