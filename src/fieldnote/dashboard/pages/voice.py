"""Customer voice: themes, sentiment, aspect x entity heatmap, emerging themes, claimed vs reported."""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from fieldnote.dashboard.components import DashContext, empty_state, md
from fieldnote.dashboard.theme import CONTEXT_GRAY, DIVERGING, mode, palette, style_figure
from fieldnote.db import repo
from fieldnote.db.models import ClaimVsReported, Theme
from fieldnote.textutil import truncate_words


def render(ctx: DashContext) -> None:
    cfg = ctx.cfg
    with ctx.db.session() as s:
        run = repo.latest_run_with(s, cfg.workspace, Theme)
        themes = repo.themes_for_run(s, cfg.workspace, run) if run else []
        tags = repo.voice_rows(s, cfg.workspace, since=ctx.now - timedelta(days=14), until=ctx.now)
        cvr_run = repo.latest_run_with(s, cfg.workspace, ClaimVsReported)
        cvr = repo.cvr_for_run(s, cfg.workspace, cvr_run) if cvr_run else []
        theme_rows = [
            {
                "id": t.id,
                "label": t.label,
                "size": t.size,
                "size_prev": t.size_prev,
                "sentiment": t.sentiment_mean,
                "emerging": t.is_emerging,
                "samples": t.sample_document_ids,
                "keywords": t.keywords,
            }
            for t in themes
        ]
        docs = repo.get_documents(s, [i for t in theme_rows for i in t["samples"][:5]])
        sample_docs = {i: (d.title, d.content, d.url, d.source_name) for i, d in docs.items()}
        tag_rows = [
            {"aspect": t.aspect, "entity": e, "sentiment": t.sentiment}
            for t, _ in tags
            for e in (t.entities or ["(no brand)"])
        ]
        cvr_rows = [
            {
                "entity": r.entity,
                "metric": r.metric,
                "unit": r.unit,
                "claimed": r.claimed_value,
                "median": r.reported_median,
                "q1": r.reported_q1,
                "q3": r.reported_q3,
                "n": r.n,
                "low": "low_confidence" in (r.flags or []),
                "samples": r.reported_values[:5],
            }
            for r in cvr
        ]
    if not theme_rows and not tag_rows:
        empty_state(
            "No customer-voice data yet.", "Reddit/YouTube collection needs API keys; the offline demo uses fixtures."
        )
        return
    if theme_rows:
        st.subheader("Themes (current vs previous 7-day window)")
        df = pd.DataFrame(theme_rows).sort_values("size")
        gray = CONTEXT_GRAY[mode()]
        fig = go.Figure()
        fig.add_bar(
            y=df["label"],
            x=df["size_prev"],
            orientation="h",
            name="Previous window",
            marker_color=gray,
            width=0.35,
            offset=-0.38,
        )
        fig.add_bar(
            y=df["label"],
            x=df["size"],
            orientation="h",
            name="Current window",
            marker_color=palette()[0],
            width=0.35,
            offset=0.02,
            customdata=df["sentiment"],
            hovertemplate="%{y}<br>%{x} posts<br>mean sentiment %{customdata:+.2f}<extra></extra>",
        )
        fig.update_layout(title="Theme volume", barmode="overlay")
        st.plotly_chart(style_figure(fig, height=80 + 38 * len(df)), width="stretch")
        emerging = [t for t in theme_rows if t["emerging"]]
        if emerging:
            st.markdown("**Emerging themes** (fast growth in share of voice from a small base)")
            for t in emerging:
                st.markdown(
                    md(f"- {t['label']}: {t['size']} posts (was {t['size_prev']}), sentiment {t['sentiment']:+.2f}")
                )
        with st.expander("Browse themes and source posts"):
            for t in sorted(theme_rows, key=lambda x: -x["size"]):
                st.markdown(
                    md(
                        f"**{t['label']}** · {t['size']} posts · sentiment {t['sentiment']:+.2f} · "
                        f"keywords: {', '.join(t['keywords'][:6])}"
                    )
                )
                for i in t["samples"][:4]:
                    if i in sample_docs:
                        _title, content, url, src = sample_docs[i]
                        st.markdown(f'- "{md(truncate_words(content, 25))}" ([{md(src)}]({url}))')
    if tag_rows:
        st.subheader("Sentiment by aspect and brand")
        tdf = pd.DataFrame(tag_rows)
        pivot = tdf.pivot_table(index="aspect", columns="entity", values="sentiment", aggfunc="mean")
        counts = tdf.pivot_table(index="aspect", columns="entity", values="sentiment", aggfunc="count")
        div = DIVERGING[mode()]
        fig = px.imshow(
            pivot,
            color_continuous_scale=[[0, div[0]], [0.5, div[1]], [1, div[2]]],
            zmin=-1,
            zmax=1,
            aspect="auto",
            labels={"color": "mean sentiment"},
            text_auto=".2f",
        )
        fig.update_traces(
            customdata=counts.values,
            hovertemplate="%{y} · %{x}<br>mean sentiment %{z:.2f}<br>n=%{customdata}<extra></extra>",
        )
        st.plotly_chart(style_figure(fig, height=90 + 34 * len(pivot), legend=False), width="stretch")
        st.caption(
            "Customer voice is a sampled, non-representative slice of public discussion; read magnitudes with care."
        )
    if cvr_rows:
        st.subheader("Claimed vs owner-reported")
        metric = cvr_rows[0]["metric"]
        rows = [r for r in cvr_rows if r["metric"] == metric and r["median"] is not None]
        if rows:
            fig = go.Figure()
            for r in sorted(rows, key=lambda x: x["n"]):
                color = CONTEXT_GRAY[mode()] if r["low"] else palette()[0]
                label = f"{r['entity']} (n={r['n']}{', low confidence' if r['low'] else ''})"
                fig.add_trace(
                    go.Scatter(
                        x=[r["q1"], r["q3"]],
                        y=[label, label],
                        mode="lines",
                        line={"color": color, "width": 3},
                        showlegend=False,
                        hoverinfo="skip",
                    )
                )
                fig.add_trace(
                    go.Scatter(
                        x=[r["median"]],
                        y=[label],
                        mode="markers",
                        marker={"size": 11, "color": color},
                        name="Reported median",
                        showlegend=False,
                        hovertemplate=f"median %{{x}} {r['unit']} (IQR {r['q1']}-{r['q3']}, n={r['n']})<extra></extra>",
                    )
                )
                if r["claimed"] is not None:
                    fig.add_trace(
                        go.Scatter(
                            x=[r["claimed"]],
                            y=[label],
                            mode="markers",
                            marker={"size": 11, "symbol": "circle-open", "color": palette()[1], "line": {"width": 2}},
                            showlegend=False,
                            hovertemplate=f"claimed %{{x}} {r['unit']}<extra></extra>",
                        )
                    )
            fig.update_layout(
                title=f"{metric.replace('_', ' ').capitalize()} ({rows[0]['unit']}): filled = reported median, bar = IQR, ring = claimed"
            )
            st.plotly_chart(style_figure(fig, height=90 + 44 * len(rows), legend=False), width="stretch")
            st.caption(
                "Rows marked low confidence have fewer reports than the configured minimum and are never stated as fact."
            )
            with st.expander("Source posts behind the reported values"):
                for r in rows:
                    st.markdown(f"**{md(r['entity'])}**")
                    for smp in r["samples"]:
                        st.markdown(
                            md(
                                f'- {smp.get("value")} {r["unit"]}: "{truncate_words(smp.get("excerpt", ""), 25)}" '
                                f"(document #{smp['document_id']})"
                            )
                        )
