"""Weekly decision memo (PDF via reportlab, plus a Markdown twin used by evals).

Sections: What happened / Why it matters / What we should do / How we execute, charts (metric trend,
theme volume, claim-vs-reported with n), an Opportunity Board table with score breakdowns, and an
appendix with the full source list, methodology and limitations. Page numbers, generated date,
workspace name and an "AI-assisted; sources linked; estimates labeled" note on every page.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from sqlalchemy import select

from fieldnote.config import WorkspaceConfig, workspace_out_dir
from fieldnote.db import repo
from fieldnote.db.engine import Database
from fieldnote.db.models import Run
from fieldnote.reports import charts
from fieldnote.reports.common import (
    Anonymizer,
    EvidenceItem,
    evidence_items,
    fixture_notice,
    jinja_env,
    verification_stats,
)
from fieldnote.scoring.opportunities import rank

METHODOLOGY = [
    "Collection: public news (RSS, Google News RSS), competitor web pages (robots.txt respected, rate-limited), "
    "Reddit and YouTube via their official APIs, and user-supplied metric files. Author identities are never stored.",
    "Change detection: normalised page text is diffed against the previous snapshot; trivial changes (timestamps, "
    "counters, banners) are ignored; changes are classified by rules first and a fast model second.",
    "Claims: every fact cites a source and a verbatim excerpt; estimates state their method and are labelled; "
    "analysis lists the claims it depends on.",
    "Verification: a deterministic critic checks sources, excerpts, numbers, names and dates; an LLM critic judges "
    "entailment and overreach and searches for counter-evidence. Rejected claims never reach outputs.",
    "Ranking: deterministic weighted scoring of market size, competitor gap, evidence strength (computed from "
    "source count, diversity, recency and primary sources) and effort. Same inputs always give the same order.",
]
LIMITATIONS = [
    "Public data only; paywalled, private or login-gated sources are not used.",
    "Customer voice is a sampled, non-representative slice of online discussion.",
    "Market and registration metrics depend entirely on what the user supplies through connectors.",
    "LLM extraction and classification can be wrong; the critic reduces but does not eliminate errors.",
    "Estimates are approximations and are labelled as such; low-sample comparisons are flagged, not asserted.",
    "This memo supports, and does not replace, human judgement.",
]


@dataclass
class MemoFinding:
    rank: int
    id: int
    title: str
    category: str
    status: str
    total: float
    scores: dict[str, float]
    what_happened: str
    why_it_matters: str
    recommended_action: str
    how_to_execute: str
    evidence: list[EvidenceItem]
    first_seen: str
    last_seen: str


@dataclass
class WeeklyMemoData:
    workspace: str
    display_name: str
    perspective: str
    week_start: str
    week_end: str
    generated_at: str
    fixture: bool
    run_ids: list[int]
    findings: list[MemoFinding] = field(default_factory=list)
    board: list[dict[str, Any]] = field(default_factory=list)
    changes: list[dict[str, Any]] = field(default_factory=list)
    themes: list[dict[str, Any]] = field(default_factory=list)
    cvr: list[dict[str, Any]] = field(default_factory=list)
    cvr_metric: str = ""
    cvr_unit: str = ""
    metric_series: dict[str, dict[str, float]] = field(default_factory=dict)
    metric_label: str = ""
    metric_unit: str = ""
    sources: list[EvidenceItem] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    disagreements: list[str] = field(default_factory=list)
    methodology: list[str] = field(default_factory=lambda: list(METHODOLOGY))
    limitations: list[str] = field(default_factory=lambda: list(LIMITATIONS))
    fixture_notice: str = ""


def build_weekly_memo(
    db: Database, cfg: WorkspaceConfig, now: datetime, run_id: int | None = None, top_n: int = 8
) -> WeeklyMemoData:
    from fieldnote.processing.metrics import metric_summaries

    start = now - timedelta(days=7)
    with db.session() as s:
        runs = list(
            s.scalars(
                select(Run).where(
                    Run.workspace == cfg.workspace,
                    Run.kind == "daily",
                    Run.as_of > start - timedelta(hours=1),
                    Run.as_of <= now,
                )
            )
        )
        run_ids = sorted({r.id for r in runs} | ({run_id} if run_id else set()))
        latest_run = max(run_ids) if run_ids else None
        summaries = metric_summaries(s, cfg)
        shares = {e.entity: e.share or 0.0 for e in summaries[0].entities} if summaries else None
        anon = Anonymizer(cfg, shares)
        data = WeeklyMemoData(
            workspace=cfg.workspace,
            display_name=cfg.display_name,
            perspective=cfg.perspective,
            week_start=cfg.local_date(start).isoformat(),
            week_end=cfg.local_date(now).isoformat(),
            generated_at=now.strftime("%Y-%m-%d %H:%M UTC"),
            fixture=cfg.fixture_mode,
            run_ids=run_ids,
            cost_usd=round(sum(r.cost_usd for r in runs), 4),
            fixture_notice=fixture_notice(),
        )
        rows = [f for f in repo.findings(s, cfg.workspace, statuses=("active", "demoted")) if f.last_seen > start]
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
        )
        n_ev = 1
        for item in ranked:
            f = item["row"]
            sc = {k: float(v) for k, v in (f.scores or {}).items() if isinstance(v, int | float)}
            data.board.append(
                {
                    "rank": item["rank"],
                    "title": anon(f.title),
                    "category": f.category,
                    "status": f.status,
                    "market_size": sc.get("market_size", 0),
                    "competitor_gap": sc.get("competitor_gap", 0),
                    "evidence_strength": sc.get("evidence_strength", 0),
                    "effort": sc.get("effort", 0),
                    "total": item["total"],
                    "evidence_count": f.evidence_count,
                }
            )
            if item["rank"] > top_n:
                continue
            ev = [e for e in evidence_items(s, list(f.evidence_claim_ids or []), start=n_ev) if e.type != "analysis"]
            n_ev += len(ev)
            data.sources.extend(ev)
            data.findings.append(
                MemoFinding(
                    rank=item["rank"],
                    id=f.id,
                    title=anon(f.title),
                    category=f.category,
                    status=f.status,
                    total=item["total"],
                    scores=sc,
                    what_happened=anon(f.what_happened),
                    why_it_matters=anon(f.why_it_matters),
                    recommended_action=anon(f.recommended_action),
                    how_to_execute=anon(f.how_to_execute),
                    evidence=ev,
                    first_seen=f.first_seen.strftime("%Y-%m-%d"),
                    last_seen=f.last_seen.strftime("%Y-%m-%d"),
                )
            )
        for c in repo.changes_between(s, cfg.workspace, start, now)[:12]:
            data.changes.append(
                {
                    "competitor": anon(c.competitor),
                    "kind": c.kind,
                    "summary": anon(c.summary),
                    "url": c.url,
                    "date": c.detected_at.strftime("%Y-%m-%d"),
                }
            )
        if latest_run:
            data.themes = [
                {
                    "label": anon(t.label),
                    "size": t.size,
                    "size_prev": t.size_prev,
                    "is_emerging": t.is_emerging,
                    "sentiment": t.sentiment_mean,
                }
                for t in repo.themes_for_run(s, cfg.workspace, latest_run)
            ]
            cvr_rows = repo.cvr_for_run(s, cfg.workspace, latest_run)
            if cvr_rows:
                data.cvr_metric = cvr_rows[0].metric
                data.cvr_unit = cvr_rows[0].unit
                data.cvr = [
                    {
                        "entity": anon(r.entity),
                        "claimed_value": r.claimed_value,
                        "reported_median": r.reported_median,
                        "reported_q1": r.reported_q1,
                        "reported_q3": r.reported_q3,
                        "n": r.n,
                        "flags": r.flags,
                    }
                    for r in cvr_rows
                    if r.metric == data.cvr_metric
                ]
        if summaries:
            data.metric_series = {anon(k): v for k, v in summaries[0].series.items()}
            data.metric_label = summaries[0].label
            data.metric_unit = summaries[0].unit
        data.verification = verification_stats(s, cfg.workspace, run_ids)
        for r in runs:
            for d in ((r.stats or {}).get("agents", {}) or {}).get("disagreements", []):
                data.disagreements.append(anon(d["description"]))
    return data


# ---------------------------------------------------------------------------------------------
# PDF rendering
# ---------------------------------------------------------------------------------------------


def _register_fonts() -> tuple[str, str]:
    """DejaVu Sans (bundled with matplotlib) covers the rupee sign and other symbols Helvetica lacks."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    try:
        import matplotlib

        font_dir = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
        if "FNSans" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont("FNSans", str(font_dir / "DejaVuSans.ttf")))
            pdfmetrics.registerFont(TTFont("FNSans-Bold", str(font_dir / "DejaVuSans-Bold.ttf")))
        return "FNSans", "FNSans-Bold"
    except Exception:
        return "Helvetica", "Helvetica-Bold"


def _p(text: str) -> str:
    return escape(text or "")


def render_weekly_memo(db: Database, cfg: WorkspaceConfig, data: WeeklyMemoData) -> dict[str, str]:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Image,
        KeepTogether,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    out_dir = workspace_out_dir(cfg.workspace) / "memos"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"weekly_{data.week_end}"
    pdf_path = out_dir / f"{stem}.pdf"
    md_path = out_dir / f"{stem}.md"
    chart_dir = out_dir / f"{stem}_charts"

    regular, bold = _register_fonts()
    ss = getSampleStyleSheet()
    ink, muted, line, accent = (
        colors.HexColor("#0b0b0b"),
        colors.HexColor("#52514e"),
        colors.HexColor("#e6e5e1"),
        colors.HexColor("#2a78d6"),
    )
    st = {
        "title": ParagraphStyle(
            "t",
            parent=ss["Title"],
            fontName=bold,
            fontSize=18,
            leading=22,
            alignment=TA_LEFT,
            textColor=ink,
            spaceAfter=4,
        ),
        "meta": ParagraphStyle("m", parent=ss["Normal"], fontName=regular, fontSize=8.5, leading=11, textColor=muted),
        "h1": ParagraphStyle(
            "h1",
            parent=ss["Heading2"],
            fontName=bold,
            fontSize=13,
            leading=16,
            textColor=ink,
            spaceBefore=10,
            spaceAfter=4,
        ),
        "h2": ParagraphStyle(
            "h2",
            parent=ss["Heading3"],
            fontName=bold,
            fontSize=10.5,
            leading=13,
            textColor=ink,
            spaceBefore=6,
            spaceAfter=2,
        ),
        "body": ParagraphStyle(
            "b", parent=ss["Normal"], fontName=regular, fontSize=9.2, leading=12.5, textColor=ink, spaceAfter=3
        ),
        "small": ParagraphStyle(
            "s", parent=ss["Normal"], fontName=regular, fontSize=7.8, leading=10, textColor=muted, spaceAfter=2
        ),
        "banner": ParagraphStyle(
            "bn",
            parent=ss["Normal"],
            fontName=bold,
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#d03b3b"),
            spaceAfter=6,
        ),
        "cell": ParagraphStyle("c", parent=ss["Normal"], fontName=regular, fontSize=7.8, leading=9.5, textColor=ink),
    }
    story: list[Any] = []
    story.append(Paragraph(_p(f"Weekly decision memo: {data.display_name}"), st["title"]))
    story.append(
        Paragraph(
            _p(
                f"Week {data.week_start} to {data.week_end} · Perspective: {data.perspective} · Generated {data.generated_at}"
            ),
            st["meta"],
        )
    )
    if data.fixture:
        story.append(Spacer(1, 4))
        story.append(Paragraph(_p(f"FIXTURE DATA. {data.fixture_notice}"), st["banner"]))
    story.append(Spacer(1, 6))

    def src_refs(f: MemoFinding) -> str:
        return ", ".join(f"[{e.label[0]}{e.n}]" for e in f.evidence) or "no sources"

    story.append(Paragraph("What happened", st["h1"]))
    if not data.findings and not data.changes:
        story.append(Paragraph("No verified findings or page changes this week.", st["body"]))
    for f in data.findings:
        story.append(
            Paragraph(
                f"<b>{_p(f'{f.rank}. {f.title}')}</b> <font color='#52514e'>({_p(f.category)}, score {f.total:.2f})</font>",
                st["body"],
            )
        )
        story.append(Paragraph(_p(f"{f.what_happened} {src_refs(f)}"), st["body"]))
    if data.changes:
        story.append(Paragraph("Competitor page changes", st["h2"]))
        for c in data.changes:
            story.append(Paragraph(f"• {_p(c['date'])}: {_p(c['summary'])}", st["body"]))

    story.append(Paragraph("Why it matters", st["h1"]))
    for f in data.findings:
        story.append(Paragraph(f"<b>{_p(f.title)}</b> [Analysis] {_p(f.why_it_matters)}", st["body"]))
    story.append(Paragraph("What we should do", st["h1"]))
    for f in data.findings:
        story.append(Paragraph(f"<b>{_p(f.title)}</b>: {_p(f.recommended_action)}", st["body"]))
    story.append(Paragraph("How we execute", st["h1"]))
    for f in data.findings:
        story.append(Paragraph(f"<b>{_p(f.title)}</b>: {_p(f.how_to_execute)}", st["body"]))

    chart_paths = [
        charts.metric_trend_chart(
            data.metric_series, data.metric_label or "metric", data.metric_unit, chart_dir / "metric_trend.png"
        ),
        charts.theme_volume_chart(data.themes, chart_dir / "theme_volume.png"),
        charts.claim_vs_reported_chart(
            data.cvr, data.cvr_metric or "metric", data.cvr_unit, chart_dir / "claim_vs_reported.png"
        ),
    ]
    imgs = [p for p in chart_paths if p is not None]
    if imgs:
        story.append(PageBreak())
        story.append(Paragraph("Charts", st["h1"]))
        for p in imgs:
            from PIL import Image as PILImage

            with PILImage.open(p) as im:
                w, h = im.size
            width = 170 * mm
            story.append(KeepTogether([Image(str(p), width=width, height=width * h / w), Spacer(1, 6)]))
        if data.cvr:
            story.append(
                Paragraph(
                    "Claim-vs-reported comparisons with n below the configured minimum are low confidence and are not stated as fact.",
                    st["small"],
                )
            )

    story.append(Paragraph("Opportunity Board", st["h1"]))
    if data.board:
        header = ["#", "Finding", "Type", "Market", "Gap", "Evidence", "Effort", "Total"]
        rows = [header] + [
            [
                str(b["rank"]),
                Paragraph(_p(b["title"]) + (" <i>(thin evidence)</i>" if b["status"] == "demoted" else ""), st["cell"]),
                b["category"],
                f"{b['market_size']:.2f}",
                f"{b['competitor_gap']:.2f}",
                f"{b['evidence_strength']:.2f}",
                f"{b['effort']:.2f}",
                f"{b['total']:.3f}",
            ]
            for b in data.board[:15]
        ]
        t = Table(rows, colWidths=[8 * mm, 72 * mm, 20 * mm, 14 * mm, 14 * mm, 16 * mm, 14 * mm, 14 * mm], repeatRows=1)
        t.setStyle(
            TableStyle(
                [
                    ("FONT", (0, 0), (-1, 0), bold, 7.8),
                    ("FONT", (0, 1), (-1, -1), regular, 7.8),
                    ("TEXTCOLOR", (0, 0), (-1, 0), muted),
                    ("LINEBELOW", (0, 0), (-1, 0), 0.8, ink),
                    ("LINEBELOW", (0, 1), (-1, -1), 0.3, line),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
                ]
            )
        )
        story.append(t)
        wts = cfg.scoring.weights
        story.append(Spacer(1, 3))
        story.append(
            Paragraph(
                _p(
                    f"Scores are 0-1. Weights: market size {wts['market_size']:.2f}, competitor gap {wts['competitor_gap']:.2f}, "
                    f"evidence strength {wts['evidence_strength']:.2f}, effort {wts['effort']:.2f} (effort is inverted: lower effort scores higher)."
                ),
                st["small"],
            )
        )
    else:
        story.append(Paragraph("No findings on the board this week.", st["body"]))

    story.append(PageBreak())
    story.append(Paragraph("Appendix A: sources", st["h1"]))
    for e in data.sources:
        story.append(
            Paragraph(
                f"[{e.label[0]}{e.n}] <b>{_p(e.label)}</b>: {_p(e.text)}<br/><font color='#52514e'>{_p(e.source_name)} · {_p(e.published)} · "
                f"<link href='{_p(e.url)}' color='#2a78d6'>{_p(e.url[:90])}</link></font>",
                st["small"],
            )
        )
    if data.disagreements:
        story.append(Paragraph("Source disagreements recorded", st["h2"]))
        for x in data.disagreements:
            story.append(Paragraph(f"• {_p(x)}", st["small"]))
    story.append(Paragraph("Appendix B: methodology", st["h1"]))
    for m in data.methodology:
        story.append(Paragraph(f"• {_p(m)}", st["small"]))
    v = data.verification
    story.append(
        Paragraph(
            _p(
                f"This week: {v.get('checked', 0)} claims checked, {v.get('verified_pct', 0)}% verified, {v.get('rejected', 0)} rejected, "
                f"{v.get('unverified', 0)} withheld. LLM cost ${data.cost_usd:.2f} across {len(data.run_ids)} run(s)."
            ),
            st["small"],
        )
    )
    story.append(Paragraph("Appendix C: limitations", st["h1"]))
    for m in data.limitations:
        story.append(Paragraph(f"• {_p(m)}", st["small"]))

    def on_page(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setFont(regular, 7.5)
        canvas.setFillColor(muted)
        canvas.drawString(18 * mm, 10 * mm, f"FieldNote · {data.display_name} · generated {data.generated_at}")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
        canvas.drawString(
            18 * mm,
            A4[1] - 10 * mm,
            "AI-assisted; sources linked; estimates labeled." + ("  FIXTURE DATA" if data.fixture else ""),
        )
        canvas.setStrokeColor(accent)
        canvas.setLineWidth(1.5)
        canvas.line(18 * mm, A4[1] - 12 * mm, 40 * mm, A4[1] - 12 * mm)
        canvas.restoreState()

    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=f"FieldNote weekly memo {data.week_end}",
        author="FieldNote",
        subject=data.display_name,
    )
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    md_path.write_text(jinja_env(False).get_template("weekly_memo.md.j2").render(d=data), encoding="utf-8")
    formats = {"pdf": str(pdf_path), "md": str(md_path)}
    with db.session() as s:
        repo.upsert_brief(
            s,
            cfg.workspace,
            kind="weekly",
            path=str(pdf_path),
            run_id=max(data.run_ids) if data.run_ids else None,
            formats=formats,
            meta={
                "week_end": data.week_end,
                "findings": [f.id for f in data.findings],
                "fixture": data.fixture,
                "charts": [str(p) for p in imgs],
            },
        )
    return {f"weekly_{k}": v for k, v in formats.items()}
