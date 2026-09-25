from __future__ import annotations

from pathlib import Path

import pytest

from fieldnote.collectors.connectors.base import ConnectorContext, normalize_period, parse_number
from fieldnote.collectors.connectors.http_json import get_path
from fieldnote.collectors.connectors.registry import build_connector, discover
from fieldnote.config import ConfigError, ConnectorConfig
from fieldnote.db.models import MetricPoint
from fieldnote.processing.metrics import dataset_document, summarize_points


def _pts(rows: list[tuple[str, str, str, float]]) -> list[MetricPoint]:
    return [MetricPoint(workspace="w", connector="c", entity=e, region=r, period=p, value=v) for e, r, p, v in rows]


CCFG = ConnectorConfig(
    type="csv_file",
    name="c",
    mapping={"entity": "b", "period": "m", "value": "u"},
    unit="units/month",
    source_url="https://data.example/x",
)


def test_month_over_month_share_and_movers() -> None:
    pts = _pts(
        [
            ("A", "N", "2026-07", 100),
            ("A", "S", "2026-07", 100),
            ("A", "N", "2026-08", 110),
            ("A", "S", "2026-08", 110),
            ("B", "N", "2026-07", 50),
            ("B", "N", "2026-08", 80),
            ("C", "N", "2026-07", 2),
            ("C", "N", "2026-08", 10),
        ]
    )
    summ = summarize_points(pts, CCFG)
    ents = {e.entity: e for e in summ.entities}
    assert summ.latest_period == "2026-08" and summ.previous_period == "2026-07"
    assert ents["A"].latest == 220 and ents["A"].previous == 200
    assert ents["A"].pct_change == pytest.approx(10.0)
    assert ents["B"].pct_change == pytest.approx(60.0)
    assert summ.total_latest == 310
    assert ents["A"].share == pytest.approx(220 / 310 * 100)
    # C grew 400% but from a tiny base (< 3% of the previous total), so it is not a "mover".
    assert summ.movers[0] == "B" and "C" not in summ.movers
    assert summ.region_share["N"] == pytest.approx(200 / 310 * 100, abs=0.01)
    assert "A | all tracked regions | 2026-08: 220 units | 2026-07: 200 units" in [e.line for e in summ.entities]


def test_dataset_document_contains_lines() -> None:
    summ = summarize_points(_pts([("A", "N", "2026-07", 10), ("A", "N", "2026-08", 12)]), CCFG)
    from datetime import datetime

    doc = dataset_document(summ, datetime(2026, 9, 1))
    assert summ.entities[0].line in doc.content and summ.total_line() in doc.content


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-08", "2026-08"),
        ("2026-08-15", "2026-08"),
        ("Aug 2026", "2026-08"),
        ("08/2026", "2026-08"),
        ("202608", "2026-08"),
    ],
)
def test_normalize_period(raw: str, expected: str) -> None:
    assert normalize_period(raw) == expected


def test_parse_number() -> None:
    assert parse_number("1,24,999") == 124999
    with pytest.raises(ValueError):
        parse_number("N/A")


def test_csv_connector(tmp_path: Path, cfg_ev) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "data" / "m.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        '# provenance note\nbrand,state,month,units\nA,KA,2026-07,"1,000"\nA,KA,Aug 2026,1200\nB,KA,2026-08,bad\n',
        encoding="utf-8",
    )
    ccfg = ConnectorConfig(
        type="csv_file",
        name="m",
        path=str(p),
        mapping={"entity": "brand", "region": "state", "period": "month", "value": "units"},
    )
    recs = build_connector(ccfg, ConnectorContext(workspace=cfg_ev)).load()
    assert [(r.entity, r.period, r.value) for r in recs] == [("A", "2026-07", 1000.0), ("A", "2026-08", 1200.0)]


def test_csv_connector_missing_columns(tmp_path: Path, cfg_ev) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "data" / "m.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x,y\n1,2\n", encoding="utf-8")
    ccfg = ConnectorConfig(
        type="csv_file", path=str(p), mapping={"entity": "brand", "period": "month", "value": "units"}
    )
    with pytest.raises(ConfigError, match="lacks columns"):
        build_connector(ccfg, ConnectorContext(workspace=cfg_ev)).load()


def test_registry_and_json_paths(cfg_ev) -> None:  # type: ignore[no-untyped-def]
    assert {"csv_file", "http_json"} <= set(discover())
    with pytest.raises(ConfigError, match="unknown connector"):
        build_connector(
            ConnectorConfig(type="nope", mapping={"entity": "a", "period": "b", "value": "c"}), ConnectorContext(cfg_ev)
        )
    data = {"data": {"rows": [{"brand": {"name": "A"}, "v": 3}]}}
    assert get_path(data, "$.data.rows.0.brand.name") == "A"
    with pytest.raises(KeyError):
        get_path(data, "data.missing")
