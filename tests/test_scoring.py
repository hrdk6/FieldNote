from __future__ import annotations

import random
from datetime import datetime, timedelta

import pytest

from fieldnote.scoring.opportunities import (
    EvidenceSource,
    ScoreInput,
    evidence_strength,
    normalise_rating,
    rank,
    score,
    weighted_total,
)

NOW = datetime(2026, 9, 22)
W = {"market_size": 0.30, "competitor_gap": 0.25, "evidence_strength": 0.30, "effort": 0.15}


def test_rating_normalisation() -> None:
    assert normalise_rating(1) == 0.0 and normalise_rating(5) == 1.0 and normalise_rating(3) == 0.5
    assert normalise_rating(1, inverse=True) == 1.0 and normalise_rating(5, inverse=True) == 0.0
    assert normalise_rating(None) == 0.5


def test_evidence_strength_components() -> None:
    srcs = [
        EvidenceSource(1, "news", NOW, False),
        EvidenceSource(2, "page", NOW - timedelta(days=14), True),
        EvidenceSource(3, "reddit", NOW - timedelta(days=28), False),
        EvidenceSource(3, "reddit", NOW - timedelta(days=28), False),  # duplicate source counts once
    ]
    total, parts = evidence_strength(srcs, NOW, 14)
    assert parts["sources"] == pytest.approx(3 / 5)
    assert parts["diversity"] == 1.0
    assert parts["primary"] == 1.0
    assert parts["recency"] == pytest.approx((1 + 0.5 + 0.25) / 3, abs=1e-3)
    assert 0 < total <= 1
    assert evidence_strength([], NOW, 14)[0] == 0.0


def test_primary_and_recency_raise_strength() -> None:
    old = [EvidenceSource(1, "news", NOW - timedelta(days=60), False)]
    fresh = [EvidenceSource(1, "news", NOW, True)]
    assert evidence_strength(fresh, NOW, 14)[0] > evidence_strength(old, NOW, 14)[0]


def test_weighted_total_and_score() -> None:
    inp = ScoreInput(
        "x", {"market_size": 5, "competitor_gap": 1, "effort": 1}, [EvidenceSource(1, "news", NOW, True)], 1, NOW
    )
    res = score(inp, W, NOW, 14)
    assert res.scores["market_size"] == 1.0 and res.scores["competitor_gap"] == 0.0 and res.scores["effort"] == 1.0
    assert res.total == pytest.approx(weighted_total(res.scores, W))


def _items() -> list[dict]:
    base = NOW
    return [
        {
            "id": 1,
            "title": "Alpha",
            "scores": {"market_size": 0.5, "competitor_gap": 0.5, "evidence_strength": 0.5, "effort": 0.5},
            "evidence_count": 2,
            "last_seen": base,
        },
        {
            "id": 2,
            "title": "Beta",
            "scores": {"market_size": 0.5, "competitor_gap": 0.5, "evidence_strength": 0.5, "effort": 0.5},
            "evidence_count": 3,
            "last_seen": base - timedelta(days=1),
        },
        {
            "id": 3,
            "title": "Gamma",
            "scores": {"market_size": 0.5, "competitor_gap": 0.5, "evidence_strength": 0.5, "effort": 0.5},
            "evidence_count": 3,
            "last_seen": base,
        },
        {
            "id": 4,
            "title": "Delta",
            "scores": {"market_size": 1.0, "competitor_gap": 0.0, "evidence_strength": 0.2, "effort": 0.0},
            "evidence_count": 1,
            "last_seen": base,
        },
        {
            "id": 5,
            "title": "Epsilon",
            "scores": {"market_size": 0.0, "competitor_gap": 0.2, "evidence_strength": 1.0, "effort": 0.9},
            "evidence_count": 5,
            "last_seen": base,
        },
    ]


def test_tie_breaks_evidence_then_recency() -> None:
    ranked = rank(_items(), W)
    ids = [x["id"] for x in ranked if x["title"] in ("Alpha", "Beta", "Gamma")]
    # Equal totals: more evidence first (Gamma/Beta have 3), then the more recent (Gamma), then Alpha.
    assert ids == [3, 2, 1]


def test_ranking_is_deterministic_under_shuffle() -> None:
    first = [x["id"] for x in rank(_items(), W)]
    for seed in range(5):
        items = _items()
        random.Random(seed).shuffle(items)
        assert [x["id"] for x in rank(items, W)] == first


def test_weight_change_reranks_as_expected() -> None:
    by_market = rank(_items(), {"market_size": 1, "competitor_gap": 0, "evidence_strength": 0, "effort": 0})
    by_evidence = rank(_items(), {"market_size": 0, "competitor_gap": 0, "evidence_strength": 1, "effort": 0})
    assert by_market[0]["title"] == "Delta"
    assert by_evidence[0]["title"] == "Epsilon"
    assert all(x["rank"] == i for i, x in enumerate(by_market, start=1))
