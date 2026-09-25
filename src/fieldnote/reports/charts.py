"""Static charts for the weekly memo (matplotlib, print/light surface).

Palette: the validated default categorical palette, assigned in fixed order (never cycled), text in
text tokens (never the series colour), hairline recessive grid, 2px lines, thin bars, one y-axis.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib.patches import Patch

SURFACE = "#fcfcfb"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID = "#e6e5e1"
CONTEXT_GRAY = "#b9b8b2"
CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
MAX_SERIES = 4  # beyond this, fold into "Other"


def _style(ax: Any, title: str, subtitle: str = "") -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=TEXT_SECONDARY, labelsize=8, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.8, linestyle="-")
    ax.set_axisbelow(True)
    ax.set_title(title, loc="left", fontsize=10.5, color=TEXT_PRIMARY, fontweight="bold", pad=14 if subtitle else 8)
    if subtitle:
        ax.text(0, 1.02, subtitle, transform=ax.transAxes, fontsize=8, color=TEXT_SECONDARY, va="bottom")


def _fig(width: float = 6.8, height: float = 2.8) -> tuple[Any, Any]:
    fig, ax = plt.subplots(figsize=(width, height), dpi=160)
    fig.patch.set_facecolor(SURFACE)
    return fig, ax


def _save(fig: Any, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
    return path


def metric_trend_chart(series: dict[str, dict[str, float]], label: str, unit: str, path: Path) -> Path | None:
    """Line chart of the top entities over time; the rest fold into 'Other'."""
    if not series:
        return None
    periods = sorted({p for s in series.values() for p in s})
    if len(periods) < 2:
        return None
    latest = periods[-1]
    ranked = sorted(series, key=lambda e: (-series[e].get(latest, 0.0), e))
    top = ranked[:MAX_SERIES]
    rest = ranked[MAX_SERIES:]
    fig, ax = _fig()
    for i, ent in enumerate(top):
        ys = [series[ent].get(p, 0.0) for p in periods]
        ax.plot(
            periods, ys, color=CATEGORICAL[i], linewidth=2, solid_capstyle="round", solid_joinstyle="round", label=ent
        )
        ax.plot(
            [periods[-1]],
            [ys[-1]],
            "o",
            color=CATEGORICAL[i],
            markersize=6,
            markeredgecolor=SURFACE,
            markeredgewidth=1.5,
        )
    if rest:
        ys = [sum(series[e].get(p, 0.0) for e in rest) for p in periods]
        ax.plot(periods, ys, color=CONTEXT_GRAY, linewidth=2, label=f"Other ({len(rest)})")
    _style(ax, f"{label.capitalize()} by entity", f"Summed across tracked regions · {unit}")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.legend(
        frameon=False,
        fontsize=8,
        labelcolor=TEXT_PRIMARY,
        ncol=min(5, len(top) + bool(rest)),
        loc="upper left",
        bbox_to_anchor=(0, -0.12),
    )
    return _save(fig, path)


def theme_volume_chart(themes: list[dict[str, Any]], path: Path) -> Path | None:
    """Horizontal bars: current window (blue) against the previous window (context gray)."""
    rows = [t for t in themes if t.get("size", 0) or t.get("size_prev", 0)][:8]
    if not rows:
        return None
    rows = list(reversed(rows))
    labels = [(t["label"][:38] + ("  · emerging" if t.get("is_emerging") else "")) for t in rows]
    fig, ax = _fig(height=0.42 * len(rows) + 1.1)
    y = list(range(len(rows)))
    h = 0.34
    ax.barh(
        [v + h / 2 + 0.02 for v in y], [t["size"] for t in rows], height=h, color=CATEGORICAL[0], label="Current window"
    )
    ax.barh(
        [v - h / 2 - 0.02 for v in y],
        [t["size_prev"] for t in rows],
        height=h,
        color=CONTEXT_GRAY,
        label="Previous window",
    )
    for v, t in zip(y, rows, strict=True):
        ax.text(t["size"] + 0.2, v + h / 2 + 0.02, str(t["size"]), va="center", fontsize=7.5, color=TEXT_PRIMARY)
    ax.set_yticks(y, labels, fontsize=8, color=TEXT_PRIMARY)
    _style(ax, "Customer-voice theme volume", "Posts per theme, current vs previous 7-day window")
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.legend(frameon=False, fontsize=8, labelcolor=TEXT_PRIMARY, loc="upper left", bbox_to_anchor=(0, -0.06), ncol=2)
    return _save(fig, path)


def claim_vs_reported_chart(rows: list[dict[str, Any]], metric: str, unit: str, path: Path) -> Path | None:
    """Dot plot: claimed value (hollow ring) vs owner-reported median with IQR whisker; n shown.

    Low-confidence rows (n below the minimum) are drawn gray and hatched, and labelled.
    """
    rows = [r for r in rows if r.get("reported_median") is not None][:8]
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: (-(r.get("n") or 0), r["entity"]))
    fig, ax = _fig(height=0.5 * len(rows) + 1.2)
    y = list(range(len(rows)))[::-1]
    for yi, r in zip(y, rows, strict=True):
        low = "low_confidence" in (r.get("flags") or [])
        color = CONTEXT_GRAY if low else CATEGORICAL[0]
        q1, q3, med = r.get("reported_q1"), r.get("reported_q3"), r["reported_median"]
        if q1 is not None and q3 is not None:
            ax.plot([q1, q3], [yi, yi], color=color, linewidth=2, solid_capstyle="round")
        ax.plot([med], [yi], "o", color=color, markersize=7, markeredgecolor=SURFACE, markeredgewidth=1.5)
        if r.get("claimed_value") is not None:
            ax.plot(
                [r["claimed_value"]],
                [yi],
                "o",
                markersize=7,
                markerfacecolor=SURFACE,
                markeredgecolor=CATEGORICAL[1],
                markeredgewidth=2,
            )
        note = f"n={r.get('n', 0)}" + (" · low confidence" if low else "")
        xmax = max(v for v in (q3, med, r.get("claimed_value")) if v is not None)
        ax.text(xmax * 1.02 + 0.5, yi, note, va="center", fontsize=7.5, color=TEXT_SECONDARY)
    ax.set_yticks(y, [r["entity"] for r in rows], fontsize=8, color=TEXT_PRIMARY)
    _style(
        ax,
        f"Claimed vs owner-reported {metric.replace('_', ' ')}",
        f"{unit}; dot = reported median, bar = IQR, ring = claimed",
    )
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    xs = [v for r in rows for v in (r.get("claimed_value"), r.get("reported_q3")) if v is not None]
    ax.set_xlim(left=0, right=max(xs) * 1.25 if xs else None)
    ax.legend(
        handles=[
            Patch(facecolor=CATEGORICAL[0], label="Reported median + IQR"),
            Patch(facecolor=SURFACE, edgecolor=CATEGORICAL[1], linewidth=2, label="Claimed"),
            Patch(facecolor=CONTEXT_GRAY, hatch="////", label="Low confidence (n below minimum)"),
        ],
        frameon=False,
        fontsize=7.5,
        labelcolor=TEXT_PRIMARY,
        loc="upper left",
        bbox_to_anchor=(0, -0.14),
        ncol=3,
    )
    return _save(fig, path)
