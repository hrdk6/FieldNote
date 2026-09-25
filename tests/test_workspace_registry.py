"""Workspace registry (files + database copies), archive/restore, and the `fieldnote workspace` CLI."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fieldnote.cli import _workspace_names, app
from fieldnote.config import ConfigError
from fieldnote.db.engine import Database
from fieldnote.workspaces import archive, exists, get_yaml, list_entries, list_names, load, restore, save

REPO = Path(__file__).resolve().parents[1]
runner = CliRunner()

NEW_YAML = """workspace: coffee_chains_india
display_name: "Coffee chains, India"
sector: "coffee chains"
region: "India"
timezone: "Asia/Kolkata"
perspective: "market-level observer"
competitors:
  - {name: "Brew One", aliases: ["BrewOne"], pages: []}
aspects: ["price", "taste"]
sources:
  news:
    search_queries: ["coffee chain", "Brew One"]
"""


@pytest.fixture
def wsdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private copy of workspaces/ so tests never write into the repository."""
    d = tmp_path / "workspaces"
    d.mkdir()
    for f in (REPO / "workspaces").glob("*.yaml"):
        shutil.copy(f, d / f.name)
    monkeypatch.setenv("FIELDNOTE_WORKSPACES_DIR", str(d))
    return d


def test_save_list_load_and_precedence(wsdir: Path, db: Database) -> None:
    res = save(NEW_YAML, origin="builder", description="coffee", db=db)
    assert res.file_path == wsdir / "coffee_chains_india.yaml" and res.file_error is None
    assert "coffee_chains_india" in list_names(db)
    text, source = get_yaml("coffee_chains_india", db)
    assert source == "database" and text == NEW_YAML  # row supersedes the identical file
    # A later edit to the file (e.g. by hand, or a newer commit) wins over the stored row.
    edited = NEW_YAML.replace('display_name: "Coffee chains, India"', 'display_name: "Cafes, India"')
    (wsdir / "coffee_chains_india.yaml").write_text(edited, encoding="utf-8")
    assert get_yaml("coffee_chains_india", db)[1] == "file"
    assert load("coffee_chains_india", db).display_name == "Cafes, India"
    # A dashboard edit that cannot write the file (read-only disk) supersedes that file version.
    newer = NEW_YAML.replace('"coffee chains"', '"specialty coffee chains"')
    save(newer, origin="edit", db=db, overwrite=True, write_file=False)
    assert get_yaml("coffee_chains_india", db) == (newer, "database")
    assert load("coffee_chains_india", db).sector == "specialty coffee chains"
    with pytest.raises(ConfigError, match="already exists"):
        save(NEW_YAML, origin="builder", db=db)


def test_database_only_workspace_and_unwritable_folder(
    wsdir: Path, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: Path, *a: object, **k: object) -> int:
        raise PermissionError(13, "Read-only file system")

    monkeypatch.setattr(Path, "write_text", refuse)
    res = save(NEW_YAML, origin="builder", db=db)
    monkeypatch.undo()
    assert res.file_path is None and "Read-only" in (res.file_error or "")
    assert not (wsdir / "coffee_chains_india.yaml").exists()
    assert get_yaml("coffee_chains_india", db)[1] == "database"
    assert load("coffee_chains_india", db).competitors[0].name == "Brew One"


def test_archive_and_restore(wsdir: Path, db: Database) -> None:
    save(NEW_YAML, origin="builder", db=db)
    archive("coffee_chains_india", db)
    assert not (wsdir / "coffee_chains_india.yaml").exists()
    assert (wsdir / "_archived" / "coffee_chains_india.yaml").exists()
    assert "coffee_chains_india" not in list_names(db)
    assert exists("coffee_chains_india", db)
    with pytest.raises(ConfigError, match="archived"):
        load("coffee_chains_india", db)
    status = {e.name: e.status for e in list_entries(db, include_archived=True)}
    assert status["coffee_chains_india"] == "archived"
    restore("coffee_chains_india", db)
    assert (wsdir / "coffee_chains_india.yaml").exists()
    assert load("coffee_chains_india", db).workspace == "coffee_chains_india"
    # A file-only (built-in) workspace can be archived too.
    archive("budget_smartphones_india", db)
    assert "budget_smartphones_india" not in list_names(db)
    restore("budget_smartphones_india", db)
    assert "budget_smartphones_india" in list_names(db)


def test_invalid_and_unsafe_names(wsdir: Path, db: Database) -> None:
    (wsdir / "broken_ws.yaml").write_text("workspace: broken_ws\ndisplay_name: x\n", encoding="utf-8")
    entry = next(e for e in list_entries(db) if e.name == "broken_ws")
    assert not entry.valid and "competitors" in entry.error
    for bad in ("../etc", "Bad", "a/b"):
        with pytest.raises(ConfigError):
            get_yaml(bad, db)
    with pytest.raises(ConfigError, match="not found"):
        get_yaml("nope_nope", db)
    with pytest.raises(ConfigError):
        save("workspace: x\n", origin="import", db=db)


def test_workspace_cli(wsdir: Path, tmp_path: Path) -> None:
    r = runner.invoke(
        app, ["workspace", "new", "Quick commerce in India: Blinkit, Zepto, BigBasket", "--no-verify", "--yes"]
    )
    assert r.exit_code == 0, r.output
    assert (wsdir / "quick_commerce_india.yaml").exists()
    r = runner.invoke(app, ["workspace", "list"])
    assert r.exit_code == 0 and "quick_commerce_india" in r.output and "ev_two_wheelers_india" in r.output
    r = runner.invoke(app, ["workspace", "show", "quick_commerce_india"])
    assert r.exit_code == 0 and "Blinkit" in r.output
    r = runner.invoke(app, ["workspace", "new", "Solid-state batteries", "--no-verify", "--print"])
    assert r.exit_code == 0 and "workspace: solid_state_batteries" in r.output
    assert not (wsdir / "solid_state_batteries.yaml").exists()
    f = tmp_path / "import.yaml"
    f.write_text(NEW_YAML, encoding="utf-8")
    assert runner.invoke(app, ["workspace", "import", str(f)]).exit_code == 0
    assert runner.invoke(app, ["workspace", "import", str(f)]).exit_code == 1  # already exists
    assert runner.invoke(app, ["workspace", "archive", "coffee_chains_india", "--yes"]).exit_code == 0
    assert runner.invoke(app, ["workspace", "restore", "coffee_chains_india"]).exit_code == 0
    assert runner.invoke(app, ["workspace", "show", "no_such_ws"]).exit_code == 1
    # Offline runs over "all workspaces" skip the ones without demo fixtures.
    assert _workspace_names(None, True, offline=True) == ["budget_smartphones_india", "ev_two_wheelers_india"]
    assert "quick_commerce_india" in _workspace_names(None, True, offline=False)
    r = runner.invoke(app, ["remind", "--all-workspaces", "--offline", "--dry-run"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["purge", "--all-workspaces", "--dry-run"])
    assert r.exit_code == 0 and "quick_commerce_india" in r.output
