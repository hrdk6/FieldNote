"""Opportunity Board scoring: deterministic, configurable, re-rankable without LLM calls.

Criteria are normalised to 0-1:
* market_size, competitor_gap: analyst 1-5 ratings -> (r - 1) / 4 (rationale kept separately)
* effort: inverse rating -> (5 - r) / 4 (lower effort scores higher)
* evidence_strength: computed in code from distinct verified sources, source-type diversity,
  recency decay (half-life), and presence of at least one primary source.
total = sum(weight_i * score_i). Ties break by evidence count, then recency, then title, then id.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

CRITERIA = ("market_size", "competitor_gap", "evidence_strength", "effort")

# Sub-weights of evidence_strength (sum to 1).
EVIDENCE_WEIGHTS = {"sources": 0.35, "diversity": 0.20, "recency": 0.25, "primary": 0.20}
SOURCES_SATURATION = 5
DIVERSITY_SATURATION = 3


@dataclass(frozen=True)
class EvidenceSource:
    document_id: int
    source_type: str
    published_at: datetime | None
    is_primary: bool


@dataclass
class ScoreInput:
    title: str
    ratings: Mapping[str, int]
    sources: list[EvidenceSource]
    evidence_count: int
    last_seen: datetime
    id: int = 0


@dataclass
class ScoreResult:
    scores: dict[str, float]
    total: float
    evidence_breakdown: dict[str, float] = field(default_factory=dict)


def normalise_rating(value: int | float | None, inverse: bool = False) -> float:
    if value is None:
        return 0.5
    v = max(1.0, min(5.0, float(value)))
    return round((5.0 - v) / 4.0 if inverse else (v - 1.0) / 4.0, 4)


def evidence_strength(
    sources: Iterable[EvidenceSource], now: datetime, half_life_days: float
) -> tuple[float, dict[str, float]]:
    uniq: dict[int, EvidenceSource] = {}
    for src in sources:
        uniq.setdefault(src.document_id, src)
    items = list(uniq.values())
    if not items:
        return 0.0, {"sources": 0.0, "diversity": 0.0, "recency": 0.0, "primary": 0.0}
    s_sources = min(len(items) / SOURCES_SATURATION, 1.0)
    s_div = min(len({i.source_type for i in items}) / DIVERSITY_SATURATION, 1.0)
    decays = []
    for i in items:
        age = max(0.0, (now - i.published_at).total_seconds() / 86400.0) if i.published_at else 2 * half_life_days
        decays.append(math.pow(0.5, age / half_life_days))
    s_rec = sum(decays) / len(decays)
    s_prim = 1.0 if any(i.is_primary for i in items) else 0.0
    parts = {"sources": s_sources, "diversity": s_div, "recency": s_rec, "primary": s_prim}
    total = sum(EVIDENCE_WEIGHTS[k] * v for k, v in parts.items())
    return round(total, 4), {k: round(v, 4) for k, v in parts.items()}


def criterion_scores(
    inp: ScoreInput, now: datetime, half_life_days: float
) -> tuple[dict[str, float], dict[str, float]]:
    ev, breakdown = evidence_strength(inp.sources, now, half_life_days)
    scores = {
        "market_size": normalise_rating(inp.ratings.get("market_size")),
        "competitor_gap": normalise_rating(inp.ratings.get("competitor_gap")),
        "evidence_strength": ev,
        "effort": normalise_rating(inp.ratings.get("effort"), inverse=True),
    }
    return scores, breakdown


def weighted_total(scores: Mapping[str, float], weights: Mapping[str, float]) -> float:
    wsum = sum(max(0.0, float(weights.get(c, 0.0))) for c in CRITERIA) or 1.0
    return round(sum(max(0.0, float(weights.get(c, 0.0))) * float(scores.get(c, 0.0)) for c in CRITERIA) / wsum, 6)


def score(inp: ScoreInput, weights: Mapping[str, float], now: datetime, half_life_days: float) -> ScoreResult:
    scores, breakdown = criterion_scores(inp, now, half_life_days)
    return ScoreResult(scores=scores, total=weighted_total(scores, weights), evidence_breakdown=breakdown)


def sort_key(total: float, evidence_count: int, last_seen: datetime | None, title: str, ident: int) -> tuple[Any, ...]:
    ts = last_seen.timestamp() if last_seen else 0.0
    return (-round(total, 6), -int(evidence_count), -ts, title.lower(), ident)


def rank(items: list[dict[str, Any]], weights: Mapping[str, float]) -> list[dict[str, Any]]:
    """Re-rank findings from their stored normalised criteria. Pure function; no LLM calls.

    Each item needs: ``scores`` (criterion -> 0..1), ``evidence_count``, ``last_seen``, ``title``, ``id``.
    Returns new dicts with ``total`` and ``rank`` set.
    """
    out = []
    for it in items:
        total = weighted_total(it.get("scores", {}), weights)
        out.append({**it, "total": total})
    out.sort(
        key=lambda x: sort_key(
            x["total"], x.get("evidence_count", 0), x.get("last_seen"), x.get("title", ""), x.get("id", 0)
        )
    )
    for i, x in enumerate(out, start=1):
        x["rank"] = i
    return out
