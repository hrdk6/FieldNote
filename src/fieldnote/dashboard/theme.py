"""Dashboard look and feel: palette (light and dark steps), Plotly template, and a little CSS.

Categorical colours are assigned in a fixed order by entity (never by rank), so a competitor keeps
its colour across every chart and filter. Status colours are reserved for good/warning/critical.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

CATEGORICAL_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
CATEGORICAL_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}
CONTEXT_GRAY = {"light": "#b9b8b2", "dark": "#6b6a64"}
DIVERGING = {"light": ["#d03b3b", "#f0efec", "#2a78d6"], "dark": ["#e66767", "#383835", "#3987e5"]}


def mode() -> str:
    try:
        t = st.context.theme.type  # type: ignore[attr-defined]
        return "dark" if t == "dark" else "light"
    except Exception:
        return "light"


def palette() -> list[str]:
    return CATEGORICAL_DARK if mode() == "dark" else CATEGORICAL_LIGHT


def entity_colors(entities: list[str]) -> dict[str, str]:
    """Stable entity -> colour map (config order); entities past the 8th fold to the context gray."""
    pal = palette()
    gray = CONTEXT_GRAY[mode()]
    return {e: (pal[i] if i < len(pal) else gray) for i, e in enumerate(entities)}


def style_figure(fig: Any, height: int = 360, legend: bool = True) -> Any:
    fig.update_layout(
        height=height,
        margin={"l": 8, "r": 8, "t": 36, "b": 8},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0, "title": None} if legend else None,
        showlegend=legend,
        font={"size": 13},
        hoverlabel={"font_size": 12},
    )
    fig.update_xaxes(showgrid=False, zeroline=False)
    fig.update_yaxes(gridwidth=1, zeroline=False)
    fig.update_traces(selector={"type": "scatter"}, line={"width": 2})
    return fig


CSS = """
<style>
.fn-banner {border-left: 4px solid #d03b3b; background: rgba(208,59,59,0.08); padding: 0.55rem 0.9rem;
  border-radius: 6px; margin-bottom: 0.8rem; font-size: 0.92rem;}
.fn-kpi {border: 1px solid rgba(128,128,128,0.25); border-radius: 10px; padding: 0.8rem 1rem; height: 100%;}
.fn-kpi .label {font-size: 0.8rem; opacity: 0.75; margin-bottom: 0.2rem;}
.fn-kpi .value {font-size: 1.6rem; font-weight: 600; line-height: 1.2;}
.fn-kpi .sub {font-size: 0.78rem; opacity: 0.7; margin-top: 0.15rem;}
.fn-tag {display: inline-block; font-size: 0.7rem; font-weight: 600; letter-spacing: .03em; text-transform: uppercase;
  border: 1px solid rgba(128,128,128,0.4); border-radius: 999px; padding: 0 0.45rem; margin-right: 0.35rem; opacity: 0.85;}
.fn-muted {opacity: 0.72; font-size: 0.86rem;}
.fn-empty {border: 1px dashed rgba(128,128,128,0.4); border-radius: 10px; padding: 1.2rem; text-align: center; opacity: 0.85;}
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)
