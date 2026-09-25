"""Metrics from pluggable connectors. All arithmetic happens here, in code, never in the LLM.

Computes month-over-month deltas, share by entity and by region, and top movers. For each connector
a *dataset summary document* is stored; estimate claims cite it, and the critic re-derives every
number from ``metric_points``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from fieldnote.collectors.base import PoliteHttpClient
from fieldnote.collectors.connectors.base import ConnectorContext
from fieldnote.collectors.connectors.registry import build_connector
from fieldnote.config import ConnectorConfig, WorkspaceConfig
from fieldnote.db import repo
from fieldnote.db.models import MetricPoint
from fieldnote.domain import CollectedDocument
from fieldnote.logging_setup import get_logger
from fieldnote.textutil import fmt_number, pct_change

log = get_logger(__name__)

MIN_MOVER_BASE_SHARE = 0.03  # ignore movers below 3% of the previous-period total


@dataclass
class EntityMetric:
    entity: str
    latest: float
    previous: float | None
    pct_change: float | None
    share: float | None
    line: str = ""


@dataclass
class ConnectorSummary:
    name: str
    label: str
    unit: str
    unit_short: str
    source_url: str
    periods: list[str]
    latest_period: str | None
    previous_period: str | None
    total_latest: float
    total_previous: float | None
    entities: list[EntityMetric] = field(default_factory=list)
    region_share: dict[str, float] = field(default_factory=dict)
    movers: list[str] = field(default_factory=list)
    series: dict[str, dict[str, float]] = field(default_factory=dict)  # entity -> period -> value
    dataset_document_id: int | None = None

    def total_line(self) -> str:
        prev = (
            f" | {self.previous_period}: {fmt_number(self.total_previous or 0)} {self.unit_short}"
            if self.previous_period
            else ""
        )
        return f"Total | all tracked entities | {self.latest_period}: {fmt_number(self.total_latest)} {self.unit_short}{prev}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "unit": self.unit,
            "unit_short": self.unit_short,
            "source_url": self.source_url,
            "latest_period": self.latest_period,
            "previous_period": self.previous_period,
            "total_latest": self.total_latest,
            "total_previous": self.total_previous,
            "dataset_document_id": self.dataset_document_id,
            "entities": [e.__dict__ for e in self.entities],
            "region_share": self.region_share,
            "movers": self.movers,
        }


def connector_name(c: ConnectorConfig) -> str:
    return c.name or c.type


def connector_label(c: ConnectorConfig) -> str:
    extra = c.model_extra or {}
    return str(extra.get("label") or (c.name or "volume").replace("_", " "))


def load_metrics(s: Session, cfg: WorkspaceConfig, http: PoliteHttpClient | None) -> dict[str, Any]:
    stats: dict[str, Any] = {"connectors": 0, "points": 0, "new_points": 0, "errors": []}
    for ccfg in cfg.connectors:
        stats["connectors"] += 1
        try:
            connector = build_connector(ccfg, ConnectorContext(workspace=cfg, http=http))
            records = connector.load()
        except Exception as exc:
            log.warning("connector %s failed: %s", ccfg.display_name, exc)
            stats["errors"].append(f"{ccfg.display_name}: {exc}")
            continue
        for r in records:
            created = repo.upsert_metric_point(
                s,
                cfg.workspace,
                connector=connector_name(ccfg),
                entity=r.entity,
                region=r.region or "all",
                period=r.period,
                value=r.value,
                unit=ccfg.unit,
                source_url=ccfg.source_url,
            )
            stats["points"] += 1
            stats["new_points"] += int(created)
    s.flush()
    return stats


def summarize_points(points: list[MetricPoint], ccfg: ConnectorConfig) -> ConnectorSummary:
    unit = ccfg.unit or ""
    unit_short = unit.split("/")[0].strip() or "units"
    by_entity_period: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    by_region_latest: dict[str, float] = defaultdict(float)
    periods = sorted({p.period for p in points})
    for p in points:
        by_entity_period[p.entity][p.period] += p.value
    latest = periods[-1] if periods else None
    previous = periods[-2] if len(periods) >= 2 else None
    for p in points:
        if p.period == latest:
            by_region_latest[p.region] += p.value
    total_latest = sum(v.get(latest, 0.0) for v in by_entity_period.values()) if latest else 0.0
    total_prev = sum(v.get(previous, 0.0) for v in by_entity_period.values()) if previous else None
    entities: list[EntityMetric] = []
    for ent in sorted(by_entity_period):
        series = by_entity_period[ent]
        lv = series.get(latest, 0.0) if latest else 0.0
        pv = series.get(previous) if previous else None
        pc = pct_change(pv, lv) if pv else None
        share = (lv / total_latest * 100.0) if total_latest else None
        em = EntityMetric(entity=ent, latest=lv, previous=pv, pct_change=pc, share=share)
        prev_part = f" | {previous}: {fmt_number(pv)} {unit_short}" if previous and pv is not None else ""
        em.line = f"{ent} | all tracked regions | {latest}: {fmt_number(lv)} {unit_short}{prev_part}"
        entities.append(em)
    entities.sort(key=lambda e: (-e.latest, e.entity))
    base = (total_prev or 0) * MIN_MOVER_BASE_SHARE
    movers = [
        e.entity
        for e in sorted(
            (e for e in entities if e.pct_change is not None and (e.previous or 0) >= base),
            key=lambda e: (-abs(e.pct_change or 0), e.entity),
        )
    ][:5]
    region_total = sum(by_region_latest.values()) or 1.0
    return ConnectorSummary(
        name=connector_name(ccfg),
        label=connector_label(ccfg),
        unit=unit,
        unit_short=unit_short,
        source_url=ccfg.source_url,
        periods=periods,
        latest_period=latest,
        previous_period=previous,
        total_latest=total_latest,
        total_previous=total_prev,
        entities=entities,
        region_share={r: round(v / region_total * 100, 2) for r, v in sorted(by_region_latest.items())},
        movers=movers,
        series={e: dict(sorted(v.items())) for e, v in sorted(by_entity_period.items())},
    )


def metric_summaries(s: Session, cfg: WorkspaceConfig) -> list[ConnectorSummary]:
    out = []
    for ccfg in cfg.connectors:
        pts = repo.metric_points(s, cfg.workspace, connector_name(ccfg))
        if not pts:
            continue
        summ = summarize_points(pts, ccfg)
        doc = repo.find_document(s, cfg.workspace, url=dataset_url(summ))
        summ.dataset_document_id = doc.id if doc else None
        out.append(summ)
    return out


def dataset_url(summ: ConnectorSummary) -> str:
    base = summ.source_url or f"https://fieldnote.invalid/dataset/{summ.name}"
    return f"{base}#fn-dataset-{summ.name}-{summ.latest_period}"


def dataset_document(summ: ConnectorSummary, now: datetime, fixture: bool = False) -> CollectedDocument:
    lines = [
        f"Dataset: {summ.name} ({summ.label}, {summ.unit}). Source: {summ.source_url or 'user-supplied file'}.",
        "Values are summed across tracked regions by FieldNote from the connector's rows.",
        summ.total_line(),
        *[e.line for e in summ.entities],
    ]
    return CollectedDocument(
        source_type="other",
        source_name=f"Dataset: {summ.name}",
        url=dataset_url(summ),
        canonical_url=summ.source_url or dataset_url(summ),
        title=f"{summ.label.capitalize()} summary, {summ.latest_period}",
        content="\n".join(lines),
        published_at=now,
        fetched_at=now,
        is_primary=True,
        metadata={
            "kind": "dataset",
            "connector": summ.name,
            "link": summ.source_url,
            "fixture": fixture,
            "entities": [e.entity for e in summ.entities],
        },
    )


def ensure_dataset_documents(s: Session, cfg: WorkspaceConfig, run_id: int, now: datetime) -> list[int]:
    ids = []
    for summ in metric_summaries(s, cfg):
        doc, _ = repo.upsert_document(s, cfg.workspace, dataset_document(summ, now, cfg.fixture_mode), run_id)
        ids.append(doc.id)
    return ids


def top_movers_table(summ: ConnectorSummary, n: int = 5) -> list[dict[str, Any]]:
    ents = {e.entity: e for e in summ.entities}
    return [
        {
            "entity": m,
            "latest": ents[m].latest,
            "previous": ents[m].previous,
            "pct_change": round(ents[m].pct_change or 0.0, 1),
            "share": round(ents[m].share or 0.0, 1),
        }
        for m in summ.movers[:n]
        if m in ents
    ]
