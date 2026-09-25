"""Read-only, deterministic, parameterised tools available to agents.

No tool writes to the database, sends anything, or accepts SQL. Arguments are validated with
Pydantic; unknown tools and bad arguments return an error object instead of raising. Document text
in tool results is wrapped in untrusted-content delimiters.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Any, Literal

from dateutil import parser as dtparser
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from rank_bm25 import BM25Okapi

from fieldnote.config import WorkspaceConfig
from fieldnote.db import repo
from fieldnote.db.engine import Database
from fieldnote.db.models import ClaimVsReported, Document, Theme
from fieldnote.llm.base import ToolSpec, inline_refs
from fieldnote.processing.metrics import metric_summaries
from fieldnote.textutil import tokenize, truncate_words, wrap_untrusted

MAX_CONTENT_CHARS = 6000


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchFilters(_Args):
    source_type: Literal["news", "page", "reddit", "youtube", "other"] | None = None
    entity: str | None = None
    since_days: int | None = Field(default=None, ge=1, le=365)
    primary_only: bool = False


class SearchArgs(_Args):
    query: str = Field(min_length=1, max_length=300)
    filters: SearchFilters | None = None
    k: int = Field(default=8, ge=1, le=20)


class GetDocumentArgs(_Args):
    id: int


class GetChangesArgs(_Args):
    since: str | None = None
    competitor: str | None = None
    kind: Literal["price", "feature", "launch", "policy", "dealer", "other"] | None = None
    min_significance: float = Field(default=0.0, ge=0, le=1)


class MetricSeriesArgs(_Args):
    entity: str
    region: str | None = None
    period_from: str | None = None
    period_to: str | None = None


class EmptyArgs(_Args):
    pass


class ThemesArgs(_Args):
    run: int | None = None


class FindingsArgs(_Args):
    status: Literal["active", "demoted", "stale", "all"] = "active"


TOOL_DEFS: dict[str, tuple[type[_Args], str]] = {
    "search_documents": (
        SearchArgs,
        "BM25 search (with recency boost) over collected documents. Returns ids, titles and snippets.",
    ),
    "get_document": (GetDocumentArgs, "Fetch one document's full text and metadata by id."),
    "get_changes": (
        GetChangesArgs,
        "Competitor page changes since a date, optionally filtered by competitor and kind.",
    ),
    "get_metric_series": (
        MetricSeriesArgs,
        "Monthly metric values for an entity (optionally a region) from configured connectors.",
    ),
    "get_metric_summary": (
        EmptyArgs,
        "Latest-period totals, month-over-month changes, shares and top movers per connector.",
    ),
    "get_themes": (
        ThemesArgs,
        "Customer-voice themes for a run: size, previous size, sentiment, aspects, entities, samples.",
    ),
    "get_claim_vs_reported": (EmptyArgs, "Claimed vs owner-reported values per entity with median, IQR, n and flags."),
    "get_findings": (FindingsArgs, "Existing findings on the Opportunity Board with status and scores."),
}


def tool_specs(include: list[str] | None = None) -> list[ToolSpec]:
    names = include or list(TOOL_DEFS)
    return [
        ToolSpec(name=n, description=TOOL_DEFS[n][1], input_schema=inline_refs(TOOL_DEFS[n][0].model_json_schema()))
        for n in names
    ]


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def document_link(d: Document) -> str:
    return str((d.meta or {}).get("link") or d.canonical_url or d.url).split("#fn-")[0]


class AgentTools:
    def __init__(self, db: Database, cfg: WorkspaceConfig, run_id: int | None, now: datetime) -> None:
        self.db = db
        self.cfg = cfg
        self.run_id = run_id
        self.now = now
        self._index: tuple[int, BM25Okapi, list[dict[str, Any]]] | None = None
        self.calls: list[dict[str, Any]] = []

    # ---- dispatch ---------------------------------------------------------------------------
    def dispatch(self, name: str, args: dict[str, Any] | None) -> dict[str, Any]:
        record: dict[str, Any] = {"tool": name, "args": args or {}}
        if name not in TOOL_DEFS:
            record["error"] = "unknown tool"
            self.calls.append(record)
            return {"tool": name, "error": f"unknown tool '{name}'. Tools are read-only: {', '.join(TOOL_DEFS)}"}
        model, _ = TOOL_DEFS[name]
        try:
            parsed = model.model_validate(args or {})
        except ValidationError as exc:
            record["error"] = "invalid arguments"
            self.calls.append(record)
            return {"tool": name, "error": f"invalid arguments: {exc.errors()[:3]}"}
        result = getattr(self, name)(parsed)
        result["tool"] = name
        record["result_size"] = len(json.dumps(result, default=str))
        self.calls.append(record)
        return result

    # ---- search -----------------------------------------------------------------------------
    def _build_index(self) -> tuple[BM25Okapi, list[dict[str, Any]]]:
        with self.db.session() as s:
            docs = repo.all_documents(s, self.cfg.workspace, until=self.now)
            if self._index and self._index[0] == len(docs):
                return self._index[1], self._index[2]
            meta = []
            corpus = []
            for d in docs:
                corpus.append(tokenize(f"{d.title} {d.title} {(d.content or d.summary)[:5000]}") or ["_empty_"])
                meta.append(
                    {
                        "id": d.id,
                        "title": d.title,
                        "source_type": d.source_type,
                        "source_name": d.source_name,
                        "published_at": d.published_at or d.fetched_at,
                        "is_primary": d.is_primary,
                        "entities": list(d.entities or []),
                        "kind": (d.meta or {}).get("kind", ""),
                        "snippet": truncate_words(d.content or d.summary, 45),
                    }
                )
        bm = BM25Okapi(corpus) if corpus else BM25Okapi([["_empty_"]])
        self._index = (len(meta), bm, meta)
        return bm, meta

    def search_documents(self, a: SearchArgs) -> dict[str, Any]:
        bm, meta = self._build_index()
        if not meta:
            return {"results": [], "note": "no documents collected yet"}
        scores = bm.get_scores(tokenize(a.query))
        f = a.filters or SearchFilters()
        ranked = []
        for i, m in enumerate(meta):
            if f.source_type and m["source_type"] != f.source_type:
                continue
            if f.entity and f.entity.lower() not in [e.lower() for e in m["entities"]]:
                continue
            if f.primary_only and not m["is_primary"]:
                continue
            age = max(0.0, (self.now - m["published_at"]).total_seconds() / 86400) if m["published_at"] else 60.0
            if f.since_days and age > f.since_days:
                continue
            score = float(scores[i])
            if score <= 0:
                continue
            ranked.append((score * (1 + 0.5 * math.exp(-age / 14.0)), m))
        ranked.sort(key=lambda x: (-x[0], x[1]["id"]))
        return {
            "results": [
                {
                    "id": m["id"],
                    "title": wrap_untrusted(m["title"]),
                    "source_type": m["source_type"],
                    "source_name": m["source_name"],
                    "published_at": _iso(m["published_at"]),
                    "is_primary": m["is_primary"],
                    "entities": m["entities"],
                    "kind": m["kind"],
                    "score": round(sc, 3),
                    "snippet": wrap_untrusted(m["snippet"]),
                }
                for sc, m in ranked[: a.k]
            ]
        }

    # ---- documents --------------------------------------------------------------------------
    def get_document(self, a: GetDocumentArgs) -> dict[str, Any]:
        with self.db.session() as s:
            d = repo.get_document(s, a.id)
            if d is None or d.workspace != self.cfg.workspace:
                return {"error": f"document {a.id} not found"}
            return {
                "document": {
                    "id": d.id,
                    "title": wrap_untrusted(d.title),
                    "url": document_link(d),
                    "source_type": d.source_type,
                    "source_name": d.source_name,
                    "published_at": _iso(d.published_at or d.fetched_at),
                    "is_primary": d.is_primary,
                    "entities": list(d.entities or []),
                    "meta_kind": (d.meta or {}).get("kind", ""),
                    "injection_suspect": bool((d.meta or {}).get("injection_suspect")),
                    "content": wrap_untrusted((d.content or d.summary)[:MAX_CONTENT_CHARS]),
                }
            }

    # ---- changes ----------------------------------------------------------------------------
    def get_changes(self, a: GetChangesArgs) -> dict[str, Any]:
        since = dtparser.isoparse(a.since).replace(tzinfo=None) if a.since else self.now - timedelta(days=7)
        with self.db.session() as s:
            rows = repo.changes_between(s, self.cfg.workspace, since, self.now, a.competitor, a.kind)
            return {
                "changes": [
                    {
                        "id": c.id,
                        "competitor": c.competitor,
                        "kind": c.kind,
                        "summary": c.summary,
                        "significance": c.significance,
                        "document_id": c.document_id,
                        "url": c.url,
                        "detected_at": _iso(c.detected_at),
                        "details": {
                            k: v
                            for k, v in (c.details or {}).items()
                            if k in ("price_change_pct", "count_change", "page_kind")
                        },
                    }
                    for c in rows
                    if c.significance >= a.min_significance
                ][:40]
            }

    # ---- metrics ----------------------------------------------------------------------------
    def get_metric_series(self, a: MetricSeriesArgs) -> dict[str, Any]:
        with self.db.session() as s:
            pts = [p for p in repo.metric_points(s, self.cfg.workspace) if p.entity.lower() == a.entity.lower()]
        if a.region:
            pts = [p for p in pts if p.region.lower() == a.region.lower()]
        agg: dict[str, float] = {}
        for p in pts:
            if (a.period_from and p.period < a.period_from) or (a.period_to and p.period > a.period_to):
                continue
            agg[p.period] = agg.get(p.period, 0.0) + p.value
        return {
            "entity": a.entity,
            "region": a.region or "all",
            "series": [{"period": k, "value": v} for k, v in sorted(agg.items())],
        }

    def get_metric_summary(self, a: EmptyArgs) -> dict[str, Any]:
        with self.db.session() as s:
            summaries = metric_summaries(s, self.cfg)
        if not summaries:
            return {"connectors": [], "note": "no metrics connector configured or no data loaded"}
        return {"connectors": [sm.as_dict() for sm in summaries]}

    # ---- customer voice ---------------------------------------------------------------------
    def get_themes(self, a: ThemesArgs) -> dict[str, Any]:
        with self.db.session() as s:
            run = a.run or self.run_id or repo.latest_run_with(s, self.cfg.workspace, Theme)
            if run is None:
                return {"themes": [], "note": "no themes computed yet"}
            themes = repo.themes_for_run(s, self.cfg.workspace, run)
            out = []
            for t in themes:
                ents = sorted((t.entity_distribution or {}).items(), key=lambda kv: (-kv[1], kv[0]))
                asp = sorted((t.aspect_distribution or {}).items(), key=lambda kv: (-kv[1], kv[0]))
                out.append(
                    {
                        "id": t.id,
                        "label": t.label,
                        "keywords": t.keywords,
                        "size": t.size,
                        "size_prev": t.size_prev,
                        "wow_change": t.wow_change,
                        "is_emerging": t.is_emerging,
                        "sentiment_mean": t.sentiment_mean,
                        "aspect_distribution": t.aspect_distribution,
                        "entity_distribution": t.entity_distribution,
                        "top_aspect": asp[0][0] if asp else None,
                        "top_entity": ents[0][0] if ents else None,
                        "top_entity_count": ents[0][1] if ents else 0,
                        "sample_document_ids": t.sample_document_ids[:8],
                    }
                )
            return {"run": run, "themes": out}

    def get_claim_vs_reported(self, a: EmptyArgs) -> dict[str, Any]:
        with self.db.session() as s:
            run = self.run_id or repo.latest_run_with(s, self.cfg.workspace, ClaimVsReported)
            if run is None:
                return {"rows": [], "note": "no claim-vs-reported data yet"}
            rows = repo.cvr_for_run(s, self.cfg.workspace, run)
            return {
                "rows": [
                    {
                        "metric": r.metric,
                        "unit": r.unit,
                        "entity": r.entity,
                        "claimed_value": r.claimed_value,
                        "claimed_source_id": r.claimed_source_id,
                        "claimed_excerpt": wrap_untrusted(r.claimed_excerpt) if r.claimed_excerpt else "",
                        "reported_median": r.reported_median,
                        "reported_q1": r.reported_q1,
                        "reported_q3": r.reported_q3,
                        "n": r.n,
                        "flags": r.flags,
                        "samples": [
                            {"document_id": v["document_id"], "excerpt": v.get("excerpt", "")}
                            for v in (r.reported_values or [])[:5]
                        ],
                    }
                    for r in rows
                ]
            }

    def get_findings(self, a: FindingsArgs) -> dict[str, Any]:
        statuses = ("active", "demoted", "stale") if a.status == "all" else (a.status,)
        with self.db.session() as s:
            rows = repo.findings(s, self.cfg.workspace, statuses=statuses)
            return {
                "findings": [
                    {
                        "id": f.id,
                        "title": f.title,
                        "category": f.category,
                        "status": f.status,
                        "total_score": round(f.total_score, 3),
                        "evidence_claim_ids": f.evidence_claim_ids,
                        "last_seen": _iso(f.last_seen),
                    }
                    for f in rows
                ]
            }
