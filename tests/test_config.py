from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from fieldnote.config import (
    ConfigError,
    apply_fixture_overlay,
    list_workspaces,
    load_workspace,
    load_workspace_file,
    parse_workspace,
    resolve_database_url,
    safe_join,
)

REPO = Path(__file__).resolve().parents[1]


def _base() -> dict:
    return yaml.safe_load((REPO / "workspaces" / "ev_two_wheelers_india.yaml").read_text(encoding="utf-8"))


def test_shipped_workspaces_load() -> None:
    names = list_workspaces()
    assert {"ev_two_wheelers_india", "budget_smartphones_india"} <= set(names)
    ev = load_workspace("ev_two_wheelers_india")
    phones = load_workspace("budget_smartphones_india")
    assert ev.focal_company is None
    assert ev.perspective == "new-entrant challenger brand"
    assert ev.claim_vs_reported[0].metric == "range"
    assert phones.claim_vs_reported[0].metric == "battery_life"
    assert set(ev.aspects) != set(phones.aspects)
    assert abs(sum(ev.scoring.weights.values()) - 1.0) < 1e-9


def test_template_is_valid() -> None:
    cfg = load_workspace_file(REPO / "workspaces" / "_template.yaml")
    assert cfg.workspace == "my_workspace"


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (lambda d: d["scoring"]["weights"].update(market_size=0.9), "must sum to 1.0"),
        (lambda d: d["scoring"]["weights"].update(novelty=0.1), "unknown scoring criteria"),
        (lambda d: d.update(timezone="Mars/Olympus"), "unknown timezone"),
        (lambda d: d.update(workspace="Bad Slug"), "lowercase slug"),
        (lambda d: d.update(aspects=["range", "range"]), "unique"),
        (lambda d: d["competitors"][0]["pages"].append({"url": "not-a-url", "kind": "pricing"}), "valid http"),
        (
            lambda d: d["competitors"][0]["pages"].append({"url": "https://x.example", "kind": "blog"}),
            "competitors.0.pages",
        ),
        (lambda d: d["competitors"][1].update(aliases=["Ola S1"]), "is used by both"),
        (lambda d: d.update(aspect_keywords={"teleportation": ["x"]}), "unknown aspects"),
        (lambda d: d.update(surprise=True), "surprise"),
        (lambda d: d["connectors"][0]["mapping"].pop("value"), "missing required keys"),
    ],
)
def test_invalid_configs_give_helpful_errors(mutate, fragment: str) -> None:  # type: ignore[no-untyped-def]
    data = copy.deepcopy(_base())
    mutate(data)
    with pytest.raises(ConfigError) as exc:
        parse_workspace(data, "test.yaml")
    assert fragment in str(exc.value)
    assert "test.yaml" in str(exc.value)


def test_file_name_must_match_slug(tmp_path: Path) -> None:
    data = _base()
    p = tmp_path / "other_name.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match="must match"):
        load_workspace_file(p)


def test_unknown_workspace_lists_available() -> None:
    with pytest.raises(ConfigError, match="Available"):
        load_workspace("does_not_exist")


def test_fixture_overlay_swaps_in_fictional_brands() -> None:
    cfg = apply_fixture_overlay(load_workspace("ev_two_wheelers_india"))
    assert cfg.fixture_mode
    assert "Voltra" in cfg.competitor_names()
    assert all(p.url.startswith("https://") and ".example" in p.url for c in cfg.competitors for p in c.pages)
    assert Path(cfg.connectors[0].path or "").exists()


def test_safe_join_blocks_traversal(tmp_path: Path) -> None:
    assert safe_join(tmp_path, "a", "b.txt") == (tmp_path / "a" / "b.txt").resolve()
    with pytest.raises(ConfigError):
        safe_join(tmp_path, "..", "..", "etc", "passwd")


def test_database_url_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert resolve_database_url(offline=True).endswith("demo.db")
    assert resolve_database_url(offline=False).endswith("fieldnote.db")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    assert resolve_database_url(offline=True).startswith("postgresql")


def test_locale_and_aliases() -> None:
    cfg = load_workspace("ev_two_wheelers_india")
    assert cfg.news_locale().ceid == "IN:en"
    assert cfg.entity_aliases()["Ather"] == "Ather Energy"
    assert "km" in cfg.aspect_terms("range")
