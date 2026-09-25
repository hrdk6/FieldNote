"""Workspace library: every template is well-formed and turns into the right draft for each region."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fieldnote.builder.catalog import CATEGORIES, REGIONS, TEMPLATES, get_template, search
from fieldnote.builder.heuristic import heuristic_draft
from fieldnote.cli import app

REPO = Path(__file__).resolve().parents[1]
runner = CliRunner()


def test_catalog_is_large_unique_and_categorised() -> None:
    assert len(TEMPLATES) >= 40
    ids = [t.id for t in TEMPLATES]
    assert len(ids) == len(set(ids)) and all(re.fullmatch(r"[a-z][a-z0-9_]+", i) for i in ids)
    assert {t.category for t in TEMPLATES} == set(CATEGORIES)  # every category has topics
    for t in TEMPLATES:
        assert set(t.players) <= set(REGIONS), t.id
        for region, players in t.players.items():
            assert len(players) == len({p.lower() for p in players}) >= 2, (t.id, region)
    assert sum(1 for t in TEMPLATES for r in REGIONS if t.players_for(r)) >= 100


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.id)
def test_every_template_drafts_its_players_and_region(template) -> None:  # type: ignore[no-untyped-def]
    for region in REGIONS:
        draft = heuristic_draft({"description": template.description(region)})
        assert draft["region"] == region, (template.id, region)
        if template.players_for(region):
            assert [e["name"] for e in draft["entities"]] == list(template.players_for(region)), region
    # Any other region works too (the model picks the players).
    other = heuristic_draft({"description": template.description("Germany")})
    assert other["region"] == "Germany" and other["timezone"] == "Europe/Berlin"


def test_search_and_lookup() -> None:
    assert [t.id for t in search("zomato")] == ["food_delivery"]
    assert all(t.category == "Finance" for t in search("", "Finance"))
    assert search("no such thing anywhere") == []
    assert get_template("airlines").description("India") == "Airlines in India: IndiGo, Air India, Akasa Air, SpiceJet"
    assert get_template("generative_ai").description("Global") == "Generative AI assistants and chatbots worldwide"
    with pytest.raises(KeyError):
        get_template("nope")


def test_cli_templates_and_new_from_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = tmp_path / "ws"
    d.mkdir()
    for f in (REPO / "workspaces").glob("*.yaml"):
        shutil.copy(f, d / f.name)
    monkeypatch.setenv("FIELDNOTE_WORKSPACES_DIR", str(d))
    r = runner.invoke(app, ["workspace", "templates", "--category", "Finance", "--region", "United Kingdom"])
    assert r.exit_code == 0 and "stock_trading_apps" in r.output and "Trading 212" in r.output
    assert runner.invoke(app, ["workspace", "templates", "--category", "Nope"]).exit_code == 1
    r = runner.invoke(
        app, ["workspace", "new", "--template", "airlines", "--region", "United Kingdom", "--no-verify", "--yes"]
    )
    assert r.exit_code == 0, r.output
    text = (d / "airlines_united_kingdom.yaml").read_text(encoding="utf-8")
    assert "British Airways" in text and "Europe/London" in text
    assert runner.invoke(app, ["workspace", "new", "--template", "nope", "--no-verify"]).exit_code == 1
    assert runner.invoke(app, ["workspace", "new", "--no-verify"]).exit_code == 1
