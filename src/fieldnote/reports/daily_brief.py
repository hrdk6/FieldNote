"""Daily brief: one page, top N findings in a fixed shape, rendered to Markdown, HTML and Telegram text.

Shape per finding: What happened -> Why it matters -> Evidence (linked) -> Recommended action -> How to
execute. Plus: what changed (page diffs), customer-voice shifts, metric movers, actions due, and a
verification footer. Sections carry ``<!-- fn:section -->`` markers so evals can trace every number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fieldnote.config import WorkspaceConfig, workspace_out_dir
from fieldnote.db import repo
from fieldnote.db.engine import Database
from fieldnote.reports.common import (
    Anonymizer,
    EvidenceItem,
    evidence_items,
    fixture_notice,
    jinja_env,
    limit_prose,
    split_telegram,
    tg_escape,
    tg_link,
    verification_stats,
)
from fieldnote.scoring.opportunities import rank
from fieldnote.textutil import word_count


@dataclass
class BriefFinding:
    id: int
    rank: int
    title: str
    category: str
    status: str
    total: float
    scores: dict[str, float]
    what_happened: str
    what_label: str
    why_it_matters: str
    recommended_action: str
    how_to_execute: str
    evidence: list[EvidenceItem]
    disputed: bool = False
    counter_note: str = ""


@dataclass
class DailyBriefData:
    workspace: str
    display_name: str
    sector: str
    region: str
    perspective: str
    date: str
    run_id: int
    fixture: bool
    anonymized: bool
    findings: list[BriefFinding] = field(default_factory=list)
    changes: list[dict[str, Any]] = field(default_factory=list)
    themes: list[dict[str, Any]] = field(default_factory=list)
    movers: list[dict[str, Any]] = field(default_factory=list)
    metric_label: str = ""
    cvr_notes: list[dict[str, Any]] = field(default_factory=list)
    actions_due: list[dict[str, Any]] = field(default_factory=list)
    appendix: list[EvidenceItem] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)
    disagreements: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    prose_words: int = 0
    generated_at: str = ""
    fixture_notice: str = ""


def build_daily_brief(
    db: Database, cfg: WorkspaceConfig, run_id: int, now: datetime, cost_usd: float | None = None
) -> DailyBriefData:
    from fieldnote.processing.metrics import metric_summaries, top_movers_table

    with db.session() as s:
        run = repo.get_run(s, run_id)
        summaries = metric_summaries(s, cfg)
        shares = {e.entity: e.share or 0.0 for e in summaries[0].entities} if summaries else None
        anon = Anonymizer(cfg, shares)
        local_date = cfg.local_date(now).isoformat()
        data = DailyBriefData(
            workspace=cfg.workspace,
            display_name=cfg.display_name,
            sector=cfg.sector,
            region=cfg.region,
            perspective=cfg.perspective,
            date=local_date,
            run_id=run_id,
            fixture=cfg.fixture_mode,
            anonymized=anon.enabled,
            cost_usd=cost_usd if cost_usd is not None else (run.cost_usd if run else 0.0),
            generated_at=now.strftime("%Y-%m-%d %H:%M UTC"),
            fixture_notice=fixture_notice(),
        )
        rows = repo.findings(s, cfg.workspace, statuses=("active", "demoted"), run_id=run_id)
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
        top_n = cfg.scoring.top_n_in_brief
        budget = max(40, cfg.output.brief_max_words // max(1, min(top_n, len(ranked)) or 1))
        n_ev = 1
        for item in ranked[:top_n]:
            f = item["row"]
            ev = evidence_items(s, list(f.evidence_claim_ids or []) + list(f.analysis_claim_ids or []), start=n_ev)
            ev_facts = [e for e in ev if e.type != "analysis"][:4]
            n_ev += len(ev_facts)
            kinds = {e.type for e in ev_facts}
            what_label = "Fact" if kinds == {"fact"} else "Estimate" if kinds == {"estimate"} else "Fact + Estimate"
            prose = limit_prose(
                {
                    "what_happened": f.what_happened,
                    "why_it_matters": f.why_it_matters,
                    "recommended_action": f.recommended_action,
                    "how_to_execute": f.how_to_execute,
                },
                budget,
            )
            counter = (f.checks or {}).get("counter_evidence") or []
            data.findings.append(
                BriefFinding(
                    id=f.id,
                    rank=item["rank"],
                    title=anon(f.title),
                    category=f.category,
                    status=f.status,
                    total=item["total"],
                    scores={k: float(v) for k, v in (f.scores or {}).items() if isinstance(v, int | float)},
                    what_happened=anon(prose["what_happened"]),
                    what_label=what_label,
                    why_it_matters=anon(prose["why_it_matters"]),
                    recommended_action=anon(prose["recommended_action"]),
                    how_to_execute=anon(prose["how_to_execute"]),
                    evidence=ev_facts,
                    disputed=bool((f.checks or {}).get("disputed")),
                    counter_note=anon(counter[0].get("note", "")) if counter else "",
                )
            )
            if anon.enabled:
                data.appendix.extend(ev_facts)
        data.prose_words = sum(
            word_count(x)
            for bf in data.findings
            for x in (bf.what_happened, bf.why_it_matters, bf.recommended_action, bf.how_to_execute)
        )
        for c in repo.changes_for_run(s, cfg.workspace, run_id)[:6]:
            link = c.url
            data.changes.append(
                {
                    "competitor": anon(c.competitor),
                    "kind": c.kind,
                    "summary": anon(c.summary),
                    "url": link,
                    "significance": c.significance,
                }
            )
        themes = repo.themes_for_run(s, cfg.workspace, run_id)
        shown = sorted(themes, key=lambda t: (-t.size, t.label))[:3]
        shown += [t for t in themes if t.is_emerging and t not in shown][:2]
        for t in shown:
            delta = t.size - t.size_prev
            data.themes.append(
                {
                    "label": anon(t.label),
                    "size": t.size,
                    "size_prev": t.size_prev,
                    "delta": delta,
                    "sentiment": t.sentiment_mean,
                    "emerging": t.is_emerging,
                }
            )
        if summaries:
            data.metric_label = summaries[0].label
            data.movers = [
                {**m, "entity": anon(m["entity"]), "period": summaries[0].latest_period}
                for m in top_movers_table(summaries[0], 4)
            ]
        for r in repo.cvr_for_run(s, cfg.workspace, run_id):
            if r.reported_median is None or r.claimed_value is None:
                continue
            data.cvr_notes.append(
                {
                    "entity": anon(r.entity),
                    "metric": r.metric.replace("_", " "),
                    "unit": r.unit,
                    "median": r.reported_median,
                    "claimed": r.claimed_value,
                    "n": r.n,
                    "low": "low_confidence" in (r.flags or []),
                }
            )
        data.cvr_notes = sorted(data.cvr_notes, key=lambda x: (-x["n"], x["entity"]))[:3]
        today = datetime.strptime(local_date, "%Y-%m-%d").date()
        for a in repo.items_due(s, cfg.workspace, today + timedelta(days=cfg.reminders.due_within_days)):
            data.actions_due.append(
                {
                    "id": a.id,
                    "description": a.description,
                    "owner": a.owner,
                    "due": a.due_date.isoformat() if a.due_date else "",
                    "overdue": bool(a.due_date and a.due_date < today),
                }
            )
        data.actions_due = data.actions_due[:6]
        data.verification = verification_stats(s, cfg.workspace, [run_id])
        if run and run.stats:
            data.disagreements = [
                anon(d["description"]) for d in (run.stats.get("agents", {}) or {}).get("disagreements", [])
            ][:3]
    return data


def telegram_lines(d: DailyBriefData) -> list[str]:
    lines = [f"*{tg_escape('FieldNote daily brief')}* \\- {tg_escape(d.display_name)}", tg_escape(d.date)]
    if d.fixture:
        lines.append(f"_{tg_escape(d.fixture_notice)}_")
    lines.append("")
    if not d.findings:
        lines.append(tg_escape("No verified findings in this run."))
    for f in d.findings:
        lines.append(f"*{tg_escape(f'{f.rank}. {f.title}')}* {tg_escape(f'({f.category}, score {f.total:.2f})')}")
        lines.append(tg_escape(f"[{f.what_label}] {f.what_happened}"))
        lines.append(tg_escape(f"[Analysis] Why it matters: {f.why_it_matters}"))
        lines.append(tg_escape(f"Action: {f.recommended_action}"))
        lines.append(tg_escape(f"How: {f.how_to_execute}"))
        links = " ".join(tg_link(f"[{e.label[0]}{e.n}]", e.url) for e in f.evidence if e.url)
        if links:
            lines.append(tg_escape("Sources: ") + links)
        lines.append("")
    if d.changes:
        lines.append(f"*{tg_escape('What changed')}*")
        lines += [tg_escape(f"• {c['summary']}") for c in d.changes]
        lines.append("")
    if d.themes:
        lines.append(f"*{tg_escape('Customer voice')}*")
        for t in d.themes:
            tag = " (emerging)" if t["emerging"] else ""
            lines.append(
                tg_escape(f"• {t['label']}{tag}: {t['size']} posts ({t['delta']:+d}), sentiment {t['sentiment']:+.2f}")
            )
        lines.append("")
    if d.movers:
        lines.append(f"*{tg_escape('Metric movers')}*")
        lines += [tg_escape(f"• {m['entity']}: {m['pct_change']:+.1f}% MoM, share {m['share']:.1f}%") for m in d.movers]
        lines.append("")
    if d.actions_due:
        lines.append(f"*{tg_escape('Actions due')}*")
        lines += [
            tg_escape(f"• {a['description']} ({a['owner']}, due {a['due']}{', OVERDUE' if a['overdue'] else ''})")
            for a in d.actions_due
        ]
        lines.append("")
    v = d.verification
    lines.append(
        tg_escape(
            f"Verification: {v.get('checked', 0)} claims checked, {v.get('verified_pct', 0)}% verified, "
            f"{v.get('rejected', 0)} rejected. Run cost ${d.cost_usd:.2f}. AI-assisted; sources linked; estimates labeled."
        )
    )
    return lines


def render_daily_brief(db: Database, cfg: WorkspaceConfig, data: DailyBriefData) -> dict[str, str]:
    out_dir = workspace_out_dir(cfg.workspace) / "briefs"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"daily_{data.date}"
    md = jinja_env(False).get_template("daily_brief.md.j2").render(d=data)
    html = jinja_env(True).get_template("daily_brief.html.j2").render(d=data)
    tg_chunks = split_telegram(telegram_lines(data), cfg.output.telegram_max_chars)
    paths = {
        "md": out_dir / f"{stem}.md",
        "html": out_dir / f"{stem}.html",
        "txt": out_dir / f"{stem}.telegram.txt",
    }
    paths["md"].write_text(md, encoding="utf-8")
    paths["html"].write_text(html, encoding="utf-8")
    paths["txt"].write_text("\n\n-----8<-----\n\n".join(tg_chunks), encoding="utf-8")
    formats = {k: str(v) for k, v in paths.items()}
    with db.session() as s:
        repo.upsert_brief(
            s,
            cfg.workspace,
            kind="daily",
            path=formats["md"],
            run_id=data.run_id,
            formats=formats,
            meta={
                "findings": [f.id for f in data.findings],
                "prose_words": data.prose_words,
                "telegram_chunks": len(tg_chunks),
                "fixture": data.fixture,
                "date": data.date,
            },
        )
    return {f"daily_{k}": v for k, v in formats.items()}


def telegram_chunks_from_file(path: str | Path) -> list[str]:
    text = Path(path).read_text(encoding="utf-8")
    return [c for c in text.split("\n\n-----8<-----\n\n") if c.strip()]
