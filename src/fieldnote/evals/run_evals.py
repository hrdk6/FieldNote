"""`fieldnote eval`: run all evals, write out/eval_report.md, store results, optionally update the README."""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from fieldnote.agents.context import AgentContext
from fieldnote.config import REPO_ROOT, Settings, WorkspaceConfig, out_dir
from fieldnote.costs import CostTracker
from fieldnote.db import repo
from fieldnote.db.engine import Database, utcnow
from fieldnote.db.models import EvalResult
from fieldnote.evals.claim_evals import adversarial_catch_rate, citation_validity, number_consistency
from fieldnote.evals.extraction_evals import evaluate_extraction
from fieldnote.llm.base import LLMClient, make_llm
from fieldnote.llm.cache import LLMCacheStore
from fieldnote.logging_setup import get_logger
from fieldnote.scoring.opportunities import CRITERIA, rank

log = get_logger(__name__)

DATASETS = Path(__file__).resolve().parent / "datasets"
README_START = "<!-- EVAL_TABLE_START -->"
README_END = "<!-- EVAL_TABLE_END -->"


def thresholds() -> dict[str, float]:
    return json.loads((DATASETS / "thresholds.json").read_text(encoding="utf-8"))


@dataclass
class EvalReport:
    workspace: str
    run_id: int | None
    mode: str
    created_at: str
    metrics: dict[str, Any] = field(default_factory=dict)
    checks: dict[str, bool] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(self.checks.values()) if self.checks else False


def ranking_checks(db: Database, cfg: WorkspaceConfig, run_id: int) -> dict[str, Any]:
    with db.session() as s:
        rows = repo.findings(s, cfg.workspace, statuses=("active", "demoted"), run_id=run_id)
        items: list[dict[str, Any]] = [
            {
                "id": f.id,
                "title": f.title,
                "scores": {k: float((f.scores or {}).get(k, 0.0)) for k in CRITERIA},
                "evidence_count": f.evidence_count,
                "last_seen": f.last_seen,
            }
            for f in rows
        ]
    if len(items) < 2:
        return {
            "findings": len(items),
            "deterministic": True,
            "weight_sensitivity": True,
            "note": "fewer than two findings",
        }
    w = cfg.scoring.weights
    first = [x["id"] for x in rank(items, w)]
    second = [x["id"] for x in rank(list(items), w)]
    shuffled = list(items)
    random.Random(3).shuffle(shuffled)
    third = [x["id"] for x in rank(shuffled, w)]
    deterministic = first == second == third
    sens: dict[str, bool] = {}
    for crit in CRITERIA:
        only = {c: (1.0 if c == crit else 0.0) for c in CRITERIA}
        top = rank(items, only)[0]
        best = max(x["scores"][crit] for x in items)
        sens[crit] = abs(top["scores"][crit] - best) < 1e-9
    moved = first != [x["id"] for x in rank(items, {c: (1.0 if c == "effort" else 0.0) for c in CRITERIA})]
    return {
        "findings": len(items),
        "deterministic": deterministic,
        "weight_sensitivity": all(sens.values()),
        "per_criterion_top_is_max": sens,
        "reweighting_changes_order": moved,
    }


def cost_latency(db: Database, cfg: WorkspaceConfig, run_id: int) -> dict[str, Any]:
    with db.session() as s:
        run = repo.get_run(s, run_id)
        traces = repo.traces_for_run(s, cfg.workspace, run_id)
        if run is None:
            return {}
        by_agent: dict[str, dict[str, float]] = {}
        for t in traces:
            a = by_agent.setdefault(
                t.agent, {"steps": 0, "latency_ms": 0, "cost": 0.0, "tokens_in": 0, "tokens_out": 0}
            )
            a["steps"] += 1
            a["latency_ms"] += t.latency_ms
            a["cost"] = round(a["cost"] + t.cost, 6)
            a["tokens_in"] += t.tokens_in
            a["tokens_out"] += t.tokens_out
        duration = (run.finished_at - run.started_at).total_seconds() if run.finished_at else None
        return {
            "run_cost_usd": round(run.cost_usd, 4),
            "tokens_in": run.tokens_in,
            "tokens_out": run.tokens_out,
            "run_seconds": round(duration, 2) if duration is not None else None,
            "llm": (run.stats or {}).get("llm"),
            "by_agent": by_agent,
        }


def run_evals(
    db: Database,
    cfg: WorkspaceConfig,
    settings: Settings,
    *,
    llm: LLMClient | None = None,
    live_llm: bool = False,
    run_id: int | None = None,
    write_report: bool = True,
) -> EvalReport:
    th = thresholds()
    with db.session() as s:
        run = repo.get_run(s, run_id) if run_id else repo.last_run(s, cfg.workspace, kinds=("daily",))
        rid = run.id if run else None
        run_as_of = run.as_of if run and run.as_of else utcnow()
    llm = llm or make_llm(settings, offline=True, cache=LLMCacheStore(db, cfg.workspace), cost=CostTracker())
    report = EvalReport(
        workspace=cfg.workspace, run_id=rid, mode=llm.name, created_at=utcnow().isoformat(timespec="seconds")
    )
    if rid is None:
        report.metrics["note"] = "no completed daily run found; run `fieldnote run` (or `--offline`) first"
    else:
        ctx = AgentContext.build(cfg, db, llm, rid, run_as_of, run_as_of)
        with db.session() as s:
            report.metrics["citation_validity"] = citation_validity(s, cfg.workspace, rid)
            base = [c for c in repo.claims_for_run(s, cfg.workspace, rid) if c.agent == "researcher"]
            report.metrics["adversarial"] = adversarial_catch_rate(ctx, s, base).as_dict()
            s.rollback()
        if live_llm and settings.has_live_llm:
            live = make_llm(settings, cache=LLMCacheStore(db, cfg.workspace), cost=CostTracker(budget_usd=1.0))
            live_ctx = AgentContext.build(cfg, db, live, rid, run_as_of, run_as_of)
            with db.session() as s:
                base = [c for c in repo.claims_for_run(s, cfg.workspace, rid) if c.agent == "researcher"]
                try:
                    report.metrics["adversarial_live"] = adversarial_catch_rate(
                        live_ctx, s, base, max_claims=15
                    ).as_dict()
                except Exception as exc:
                    report.metrics["adversarial_live"] = {"error": str(exc)[:300]}
                s.rollback()
            live.cache.flush()
        with db.session() as s:
            daily = repo.latest_brief(s, cfg.workspace, "daily")
            weekly = repo.latest_brief(s, cfg.workspace, "weekly")
            paths = []
            if daily and daily.run_id == rid:
                paths.append(Path((daily.formats or {}).get("md", daily.path)))
            if weekly and (weekly.formats or {}).get("md"):
                paths.append(Path(weekly.formats["md"]))
            report.metrics["number_consistency"] = number_consistency(s, cfg, paths, rid)
            report.metrics["number_consistency"]["files"] = [p.name for p in paths]
        report.metrics["ranking"] = ranking_checks(db, cfg, rid)
        report.metrics["cost_latency"] = cost_latency(db, cfg, rid)
    report.metrics["extraction"] = evaluate_extraction(cfg, llm)
    llm.cache.flush()
    m = report.metrics
    if rid is not None:
        report.checks = {
            "citation_validity": m["citation_validity"]["rate"] >= th["citation_validity"],
            "adversarial_catch_rate": m["adversarial"]["rate"] >= th["adversarial_catch_rate"],
            "number_consistency": m["number_consistency"]["rate"] >= th["number_consistency"]
            and m["number_consistency"]["entity_rate"] >= th["number_consistency"],
            "ranking_deterministic": bool(m["ranking"]["deterministic"]),
            "weight_sensitivity": bool(m["ranking"]["weight_sensitivity"]),
        }
    report.checks["extraction_precision"] = m["extraction"]["precision"] >= th["extraction_precision"]
    report.checks["extraction_recall"] = m["extraction"]["recall"] >= th["extraction_recall"]
    if write_report:
        write_eval_report([report])
    with db.session() as s:
        s.add(
            EvalResult(
                workspace=cfg.workspace,
                run_id=rid,
                mode=report.mode,
                metrics={**report.metrics, "checks": report.checks},
                passed=report.passed,
                report_path=str(out_dir() / "eval_report.md"),
            )
        )
    return report


def summary_rows(reports: list[EvalReport]) -> list[list[str]]:
    th = thresholds()
    rows = []
    for r in reports:
        m = r.metrics
        if "citation_validity" in m:
            cv, adv, nc = m["citation_validity"], m["adversarial"], m["number_consistency"]
            rows.append(
                [
                    r.workspace,
                    "Citation validity",
                    f"{cv['rate']:.1%} ({cv['valid']}/{cv['claims']})",
                    f">= {th['citation_validity']:.0%}",
                    _ok(r, "citation_validity"),
                ]
            )
            rows.append(
                [
                    r.workspace,
                    f"Critic adversarial catch rate ({r.mode})",
                    f"{adv['rate']:.1%} ({adv['caught']}/{adv['total']})",
                    f">= {th['adversarial_catch_rate']:.0%}",
                    _ok(r, "adversarial_catch_rate"),
                ]
            )
            if "adversarial_live" in m and "rate" in m["adversarial_live"]:
                al = m["adversarial_live"]
                rows.append(
                    [
                        r.workspace,
                        "Critic adversarial catch rate (live LLM)",
                        f"{al['rate']:.1%} ({al['caught']}/{al['total']})",
                        "reported",
                        "-",
                    ]
                )
            rows.append(
                [
                    r.workspace,
                    "Output number/entity consistency",
                    f"{nc['rate']:.1%} of {nc['numbers_checked']} numbers; {nc['entity_rate']:.1%} of {nc['entities_checked']} names",
                    f">= {th['number_consistency']:.0%}",
                    _ok(r, "number_consistency"),
                ]
            )
            rk = m["ranking"]
            rows.append(
                [
                    r.workspace,
                    "Ranking determinism / weight sensitivity",
                    f"{'yes' if rk['deterministic'] else 'no'} / {'yes' if rk['weight_sensitivity'] else 'no'} ({rk['findings']} findings)",
                    "yes / yes",
                    "PASS" if r.checks.get("ranking_deterministic") and r.checks.get("weight_sensitivity") else "FAIL",
                ]
            )
            cl = m["cost_latency"]
            rows.append(
                [
                    r.workspace,
                    "Run cost / duration",
                    f"${cl.get('run_cost_usd', 0):.2f} / {cl.get('run_seconds')}s ({cl.get('llm')})",
                    f"<= ${th.get('max_run_cost_usd', 3.0):.2f}",
                    "-",
                ]
            )
        ex = m["extraction"]
        rows.append(
            [
                r.workspace,
                f"Action-item extraction ({ex['notes']} notes, {ex['gold_items']} items)",
                f"P {ex['precision']:.1%} / R {ex['recall']:.1%}; owner {ex['owner_accuracy']:.1%}, due date {ex['due_date_accuracy']:.1%}",
                f"P,R >= {th['extraction_precision']:.0%}",
                "PASS" if r.checks.get("extraction_precision") and r.checks.get("extraction_recall") else "FAIL",
            ]
        )
    return rows


def _ok(r: EvalReport, key: str) -> str:
    return "PASS" if r.checks.get(key) else "FAIL"


def markdown_table(reports: list[EvalReport]) -> str:
    lines = ["| Workspace | Eval | Result | Target | Status |", "|---|---|---|---|---|"]
    lines += ["| " + " | ".join(row) + " |" for row in summary_rows(reports)]
    return "\n".join(lines)


def write_eval_report(reports: list[EvalReport], path: Path | None = None) -> Path:
    path = path or out_dir() / "eval_report.md"
    parts = [
        "# FieldNote eval report",
        "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M} · LLM mode(s): {', '.join(sorted({r.mode for r in reports}))}",
        "",
        markdown_table(reports),
        "",
    ]
    for r in reports:
        m = r.metrics
        parts += [f"## {r.workspace} (run {r.run_id})", "", f"Overall: **{'PASS' if r.passed else 'FAIL'}**", ""]
        if "adversarial" in m:
            parts.append("Adversarial catch rate by corruption type:")
            for kind, v in sorted(m["adversarial"]["by_kind"].items()):
                parts.append(f"- {kind}: {v['caught']}/{v['total']}")
            if m["adversarial"]["missed_examples"]:
                parts.append(
                    "- missed examples: "
                    + "; ".join(f"[{x['kind']}] {x['text'][:90]}" for x in m["adversarial"]["missed_examples"][:3])
                )
            parts.append("")
        if "number_consistency" in m and m["number_consistency"]["problems"]:
            parts.append("Number/entity consistency problems:")
            parts += [f"- {p}" for p in m["number_consistency"]["problems"][:10]]
            parts.append("")
        ex = m["extraction"]
        if ex["errors"]:
            parts.append("Extraction errors (first 10):")
            parts += [f"- {e}" for e in ex["errors"][:10]]
            parts.append("")
        if "cost_latency" in m and m["cost_latency"].get("by_agent"):
            parts.append("Latency and cost by agent:")
            for agent, v in sorted(m["cost_latency"]["by_agent"].items()):
                parts.append(
                    f"- {agent}: {v['steps']} steps, {v['latency_ms']} ms, ${v['cost']:.4f}, {v['tokens_in']}/{v['tokens_out']} tokens"
                )
            parts.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def update_readme(reports: list[EvalReport], readme: Path | None = None) -> bool:
    readme = readme or REPO_ROOT / "README.md"
    if not readme.exists():
        return False
    text = readme.read_text(encoding="utf-8")
    if README_START not in text or README_END not in text:
        return False
    table = markdown_table(reports)
    note = f"_Auto-generated by `fieldnote eval --update-readme` on {datetime.now():%Y-%m-%d} ({', '.join(sorted({r.mode for r in reports}))} LLM path)._"
    new = re.sub(
        re.escape(README_START) + r".*?" + re.escape(README_END),
        f"{README_START}\n{note}\n\n{table}\n{README_END}",
        text,
        flags=re.S,
    )
    readme.write_text(new, encoding="utf-8")
    return True
