"""Dashboard: background runs, and the Workspaces page end to end via Streamlit's AppTest."""

from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from fieldnote.dashboard import runner
from fieldnote.db.engine import Database, utcnow
from fieldnote.db.models import Run

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "src" / "fieldnote" / "dashboard" / "app.py"


class FakeProc:
    def __init__(self) -> None:
        self.pid = 4242
        self.done = False

    def poll(self) -> int | None:
        return 0 if self.done else None


def test_launch_run_refuses_parallel_runs(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[list[str]] = []
    proc = FakeProc()

    def fake_popen(cmd: list[str], **kwargs: Any) -> FakeProc:
        started.append(cmd)
        assert kwargs["stdin"] is not None and "shell" not in kwargs
        return proc

    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    runner._PROCS.clear()
    info = runner.launch_run(db, "ev_two_wheelers_india")
    assert started[0][-4:] == ["run", "-w", "ev_two_wheelers_india", "--dry-run"]
    assert info.log_path.name.startswith("dashboard-run-") and runner.is_busy(db, "ev_two_wheelers_india")
    with pytest.raises(runner.RunBusyError):
        runner.launch_run(db, "ev_two_wheelers_india")
    proc.done = True
    assert not runner.is_busy(db, "ev_two_wheelers_india")
    # A run started elsewhere (scheduler, CLI) blocks too, unless it is stale (crashed hours ago).
    with db.session() as s:
        s.add(Run(workspace="ev_two_wheelers_india", run_date="2026-09-25", status="running", started_at=utcnow()))
    assert runner.active_run(db, "ev_two_wheelers_india") is not None
    with pytest.raises(runner.RunBusyError):
        runner.launch_run(db, "ev_two_wheelers_india")
    with db.session() as s:
        run = s.query(Run).filter_by(workspace="ev_two_wheelers_india").one()
        run.started_at = utcnow() - timedelta(hours=5)
    assert runner.active_run(db, "ev_two_wheelers_india") is None
    with pytest.raises(Exception, match="invalid workspace"):
        runner.launch_run(db, "../evil")
    runner._PROCS.clear()


@pytest.fixture
def empty_wsdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "ws"
    d.mkdir()
    monkeypatch.setenv("FIELDNOTE_WORKSPACES_DIR", str(d))
    return d


def test_workspaces_page_creates_a_workspace(empty_wsdir: Path) -> None:
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception, at.exception
    assert any("No workspace yet" in i.value for i in at.sidebar.info)
    at.text_area(key="fn_desc").input("Quick commerce in India: Blinkit, Zepto, BigBasket")
    at.checkbox[0].uncheck()  # skip network verification in tests
    next(b for b in at.button if b.label == "Draft workspace").click().run()
    assert not at.exception, at.exception
    assert at.text_input(key="rv_slug").value == "quick_commerce_india"
    at.checkbox(key="rv_runafter").uncheck()
    create = next(b for b in at.button if b.label == "Create workspace")
    create.click().run()
    assert not at.exception, at.exception
    assert (empty_wsdir / "quick_commerce_india.yaml").exists()
    assert any("Created 'quick_commerce_india'" in s.value for s in at.success)
    assert at.sidebar.selectbox(key="workspace").value == "quick_commerce_india"


def _workspaces_page_script() -> None:
    from fieldnote.dashboard import components as c
    from fieldnote.dashboard.pages import workspaces

    workspaces.render(c.build_context("ev_two_wheelers_india"))


def test_workspaces_page_for_an_existing_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from streamlit.testing.v1 import AppTest

    d = tmp_path / "ws"
    d.mkdir()
    for f in (REPO / "workspaces").glob("*.yaml"):
        shutil.copy(f, d / f.name)
    monkeypatch.setenv("FIELDNOTE_WORKSPACES_DIR", str(d))
    at = AppTest.from_file(str(APP), default_timeout=60).run()  # overview renders for a built-in workspace
    assert not at.exception, at.exception
    at = AppTest.from_function(_workspaces_page_script, default_timeout=60).run()
    assert not at.exception, at.exception
    assert any(b.label == "Run now" for b in at.button)
    yaml_box = next(t for t in at.text_area if t.key and t.key.startswith("fn_yaml_ev_two_wheelers_india"))
    yaml_box.input(yaml_box.value.replace("workspace: ev_two_wheelers_india", "workspace: renamed_ws")).run()
    next(b for b in at.button if b.label == "Save changes").click().run()
    assert any("the id must stay" in e.value for e in at.error)
    assert not (d / "renamed_ws.yaml").exists()


def test_password_gate(empty_wsdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("FIELDNOTE_DASHBOARD_PASSWORD", "s3cret-pass")
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception
    assert not at.text_area  # nothing but the sign-in form
    at.text_input[0].input("wrong")
    at.button[0].click().run()
    assert any("Wrong password" in e.value for e in at.error)
    at.text_input[0].input("s3cret-pass")
    at.button[0].click().run()
    assert not at.exception
    assert at.text_area(key="fn_desc") is not None


def test_dollar_amounts_are_not_typeset_as_math() -> None:
    from fieldnote.dashboard.components import esc, md

    assert md("Amazon plans $3 billion, $1 billion by 2027") == r"Amazon plans \$3 billion, \$1 billion by 2027"
    assert esc("<b>$5</b>") == "&lt;b&gt;&#36;5&lt;/b&gt;"


def test_library_template_drafts_a_workspace_for_the_chosen_region(empty_wsdir: Path) -> None:
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception, at.exception
    at.selectbox(key="fn_lib_region").set_value("United Kingdom").run()
    at.checkbox[0].uncheck()  # no network in tests
    at.button(key="fn_lib_use_food_delivery").click().run()
    assert not at.exception, at.exception
    assert at.text_input(key="rv_slug").value == "food_delivery_apps_united_kingdom"
    names = list(at.data_editor(key="rv_entities").value["name"]) if hasattr(at, "data_editor") else None
    assert names is None or names == ["Deliveroo", "Just Eat", "Uber Eats"]
    assert at.text_area(key="fn_desc").value.startswith("Food delivery apps in United Kingdom")


def test_sidebar_offers_the_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from streamlit.testing.v1 import AppTest

    from fieldnote.dashboard.components import LIBRARY_OPTION

    d = tmp_path / "ws"
    d.mkdir()
    for f in (REPO / "workspaces").glob("*.yaml"):
        shutil.copy(f, d / f.name)
    monkeypatch.setenv("FIELDNOTE_WORKSPACES_DIR", str(d))
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    box = at.sidebar.selectbox(key="workspace")
    assert box.options[-1].startswith("+ Add from the library")
    previous = box.value
    box.set_value(LIBRARY_OPTION).run()
    assert not at.exception, at.exception
    assert at.sidebar.selectbox(key="workspace").value == previous  # selection is kept
    assert any(t.label == "New workspace" for t in at.tabs)  # the Workspaces page is open
