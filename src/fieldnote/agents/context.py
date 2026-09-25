"""Shared agent context and the run-context summary handed to the researcher."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fieldnote.agents.tools import AgentTools
from fieldnote.agents.trace import Tracer
from fieldnote.config import WorkspaceConfig
from fieldnote.db import repo
from fieldnote.db.engine import Database
from fieldnote.llm.base import LLMClient
from fieldnote.processing.metrics import metric_summaries


@dataclass
class AgentContext:
    cfg: WorkspaceConfig
    db: Database
    llm: LLMClient
    tracer: Tracer
    run_id: int | None
    now: datetime
    since: datetime
    tools: AgentTools

    @classmethod
    def build(
        cls,
        cfg: WorkspaceConfig,
        db: Database,
        llm: LLMClient,
        run_id: int | None,
        now: datetime,
        since: datetime,
        tracer: Tracer | None = None,
    ) -> AgentContext:
        return cls(
            cfg=cfg,
            db=db,
            llm=llm,
            tracer=tracer or Tracer(db, cfg.workspace, run_id, llm.cost),
            run_id=run_id,
            now=now,
            since=since,
            tools=AgentTools(db, cfg, run_id, now),
        )


@dataclass
class RunContextSummary:
    new_documents: dict[str, int] = field(default_factory=dict)
    changes: list[dict[str, Any]] = field(default_factory=list)
    themes: list[dict[str, Any]] = field(default_factory=list)
    metric_movers: list[dict[str, Any]] = field(default_factory=list)
    cvr: list[dict[str, Any]] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)

    def to_prompt(self) -> str:
        lines = [
            "New documents since the last run: "
            + (", ".join(f"{k}={v}" for k, v in sorted(self.new_documents.items())) or "none")
        ]
        if self.changes:
            lines.append("Competitor page changes detected this run:")
            lines += [
                f"- change {c['id']} [{c['kind']}, significance {c['significance']:.2f}] {c['summary']}"
                for c in self.changes[:25]
            ]
        if self.themes:
            lines.append("Customer-voice themes (current window vs previous):")
            lines += [
                f"- theme {t['id']} '{t['label']}': {t['size']} posts (prev {t['size_prev']}), sentiment {t['sentiment_mean']:+.2f}"
                + (" EMERGING" if t["is_emerging"] else "")
                for t in self.themes[:12]
            ]
        if self.metric_movers:
            lines.append("Metric movers (computed in code):")
            lines += [f"- {m['connector']}: {m['entity']} {m['pct_change']:+.1f}% MoM" for m in self.metric_movers[:10]]
        if self.cvr:
            lines.append("Claimed vs reported (computed in code):")
            lines += [
                f"- {r['entity']} {r['metric']}: claimed {r['claimed_value']}, reported median {r['reported_median']} (n={r['n']}{', LOW CONFIDENCE' if 'low_confidence' in r['flags'] else ''})"
                for r in self.cvr[:12]
            ]
        return "\n".join(lines)


def build_run_context(ctx: AgentContext) -> RunContextSummary:
    cfg = ctx.cfg
    out = RunContextSummary()
    with ctx.db.session() as s:
        docs = repo.documents_between(s, cfg.workspace, ctx.since, ctx.now)
        for d in docs:
            out.new_documents[d.source_type] = out.new_documents.get(d.source_type, 0) + 1
        if ctx.run_id is not None:
            out.changes = [
                {
                    "id": c.id,
                    "kind": c.kind,
                    "significance": c.significance,
                    "summary": c.summary,
                    "competitor": c.competitor,
                }
                for c in repo.changes_for_run(s, cfg.workspace, ctx.run_id)
            ]
            out.themes = [
                {
                    "id": t.id,
                    "label": t.label,
                    "size": t.size,
                    "size_prev": t.size_prev,
                    "sentiment_mean": t.sentiment_mean,
                    "is_emerging": t.is_emerging,
                }
                for t in repo.themes_for_run(s, cfg.workspace, ctx.run_id)
            ]
            out.cvr = [
                {
                    "entity": r.entity,
                    "metric": r.metric,
                    "claimed_value": r.claimed_value,
                    "reported_median": r.reported_median,
                    "n": r.n,
                    "flags": r.flags,
                }
                for r in repo.cvr_for_run(s, cfg.workspace, ctx.run_id)
            ]
        for summ in metric_summaries(s, cfg):
            ents = {e.entity: e for e in summ.entities}
            for m in summ.movers[:3]:
                if ents[m].pct_change is not None:
                    out.metric_movers.append({"connector": summ.name, "entity": m, "pct_change": ents[m].pct_change})
    queries: list[str] = []
    for c in out.changes[:4]:
        queries.append(f"{c['competitor']} {c['kind']}")
    queries += cfg.keywords[:4]
    queries += cfg.competitor_names()[:4]
    out.queries = list(dict.fromkeys(q for q in queries if q))[:10]
    return out
