"""Shared dashboard state and widgets."""

from __future__ import annotations

import hmac
import html
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import streamlit as st

from fieldnote.config import ConfigError, WorkspaceConfig, apply_fixture_overlay, get_settings
from fieldnote.db import repo
from fieldnote.db.engine import Database, get_database, utcnow
from fieldnote.db.models import Run
from fieldnote.reports.common import EvidenceItem


def md(text: object) -> str:
    """Text for st.markdown/st.caption/labels: escape '$' so "$3 bn ... $1 bn" is not typeset as LaTeX."""
    return str(text).replace("$", "\\$")


def esc(text: object) -> str:
    """HTML-escape for unsafe_allow_html blocks, with '$' as an entity for the same reason."""
    return html.escape(str(text)).replace("$", "&#36;")


@dataclass
class DashContext:
    cfg: WorkspaceConfig
    db: Database
    run: Run | None
    fixture: bool
    now: datetime

    @property
    def run_id(self) -> int | None:
        return self.run.id if self.run else None


def database_url(workspace: str | None = None) -> str:
    """Which database a workspace's pages read.

    ``DATABASE_URL`` wins. Otherwise a workspace with live runs reads the live SQLite file, one that has
    only offline demo runs reads the demo file, and a brand-new workspace reads the live file.
    """
    import os

    from fieldnote.config import data_dir, resolve_database_url

    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    live = data_dir() / "fieldnote.db"
    demo = data_dir() / "demo.db"
    if _has_runs(live, workspace) or not _has_runs(demo, workspace):
        return resolve_database_url(offline=False)
    return resolve_database_url(offline=True)


def _has_runs(path: Any, workspace: str | None = None) -> bool:
    """True if a SQLite file exists and holds a pipeline run (for ``workspace`` if given). Read-only."""
    import sqlite3

    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as conn:
            if workspace is None:
                row = conn.execute("SELECT COUNT(*) FROM runs").fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM runs WHERE workspace = ?", (workspace,)).fetchone()
            return bool(row[0])
    except sqlite3.Error:
        return False


@st.cache_resource(show_spinner=False)
def _db(url: str) -> Database:
    return get_database(url)


def db_for(workspace: str) -> Database:
    return _db(database_url(workspace))


def registry() -> Database:
    """Database holding workspace copies (always the live one, never the demo file)."""
    from fieldnote.config import resolve_database_url

    return _db(resolve_database_url(offline=False))


def build_context(workspace: str) -> DashContext:
    from fieldnote.workspaces import load

    db = _db(database_url(workspace))
    cfg = load(workspace, registry())
    with db.session() as s:
        run = repo.last_run(s, cfg.workspace, kinds=("daily",), statuses=("success", "partial", "failed"))
        if run is not None:
            s.expunge(run)
    fixture = bool(run and (run.mode == "offline" or (run.stats or {}).get("fixture")))
    if fixture:
        try:
            cfg = apply_fixture_overlay(cfg)
        except ConfigError:
            fixture = False
    now = run.as_of if run and run.as_of else utcnow()
    return DashContext(cfg=cfg, db=db, run=run, fixture=fixture, now=now)


PENDING_WORKSPACE = "_fn_pending_workspace"
LAST_WORKSPACE = "_fn_last_workspace"
OPEN_LIBRARY = "_fn_open_library"
# Last entry of the sidebar selector: opens the workspace library instead of selecting a workspace.
LIBRARY_OPTION = "__fieldnote_library__"


def select_workspace(name: str) -> None:
    """Switch the sidebar selection on the next rerun (a widget's own key cannot be set after it renders)."""
    st.session_state[PENDING_WORKSPACE] = name


def workspace_switcher() -> str | None:
    """Sidebar selector over valid, active workspaces (files and database). None if there are none."""
    from fieldnote.workspaces import list_entries

    entries = list_entries(registry())
    valid = [e for e in entries if e.valid]
    invalid = [e for e in entries if not e.valid]
    if invalid:
        st.sidebar.warning(
            "Invalid workspace config: " + ", ".join(e.name for e in invalid) + ". Fix it under Workspaces.",
            icon=":material/error:",
        )
    names = [e.name for e in valid]
    if not names:
        return None
    from fieldnote.builder.catalog import TEMPLATES

    labels = {e.name: e.display_name for e in valid}
    labels[LIBRARY_OPTION] = f"+ Add from the library ({len(TEMPLATES)} topics)..."
    pending = st.session_state.pop(PENDING_WORKSPACE, None)
    if pending in names:
        st.session_state["workspace"] = pending
    elif st.session_state.get("workspace") not in names and st.session_state.get("workspace") != LIBRARY_OPTION:
        preferred = get_settings().default_workspace
        st.session_state["workspace"] = preferred if preferred in names else names[0]
    choice = st.sidebar.selectbox(
        "Workspace", [*names, LIBRARY_OPTION], format_func=lambda n: labels[n], key="workspace"
    )
    if choice == LIBRARY_OPTION:
        # Keep showing the previous workspace; the app opens the library page.
        previous = st.session_state.get(LAST_WORKSPACE)
        previous = previous if previous in names else names[0]
        select_workspace(previous)
        st.session_state[OPEN_LIBRARY] = True
        return str(previous)
    st.session_state[LAST_WORKSPACE] = choice
    return str(choice)


def can_edit() -> bool:
    return not get_settings().dashboard_readonly


def require_login() -> bool:
    """Password gate when FIELDNOTE_DASHBOARD_PASSWORD is set. Returns True when access is granted."""
    password = get_settings().dashboard_password
    if not password or st.session_state.get("_fn_auth") is True:
        return True
    st.title("FieldNote")
    with st.form("fn_login"):
        entered = st.text_input("Password", type="password")
        ok = st.form_submit_button("Sign in", type="primary")
    if ok:
        if hmac.compare_digest(entered.encode("utf-8"), password.encode("utf-8")):
            st.session_state["_fn_auth"] = True
            st.rerun()
        st.error("Wrong password.")
    return False


def fixture_banner(ctx: DashContext) -> None:
    if ctx.fixture:
        st.markdown(
            '<div class="fn-banner"><b>FIXTURE DATA.</b> This workspace is showing the bundled synthetic demo dataset '
            "(fictional brands, <code>.example</code> links). It is not real market information.</div>",
            unsafe_allow_html=True,
        )


def kpi(col: Any, label: str, value: str, sub: str = "") -> None:
    col.markdown(
        f'<div class="fn-kpi"><div class="label">{esc(label)}</div><div class="value">{esc(value)}</div>'
        f'<div class="sub">{esc(sub)}</div></div>',
        unsafe_allow_html=True,
    )


def empty_state(message: str, hint: str = "") -> None:
    extra = f"<div class='fn-muted' style='margin-top:.4rem'>{esc(hint)}</div>" if hint else ""
    st.markdown(f'<div class="fn-empty">{esc(message)}{extra}</div>', unsafe_allow_html=True)


def tag(text: str) -> str:
    return f'<span class="fn-tag">{esc(text)}</span>'


def evidence_list(items: list[EvidenceItem]) -> None:
    if not items:
        st.caption("No verified evidence attached.")
        return
    lines = []
    for e in items:
        quote = f' "{esc(e.quote)}"' if e.show_quote else ""
        link = (
            f'<a href="{html.escape(e.url)}" target="_blank">{esc(e.source_name or "source")}</a>'
            if e.url
            else esc(e.source_name)
        )
        more = f" +{e.extra_sources} more" if e.extra_sources else ""
        lines.append(f"<li>{tag(e.label)}{esc(e.text)}{quote} ({link}, {esc(e.published)}){more}</li>")
    st.markdown("<ul style='padding-left:1.1rem'>" + "".join(lines) + "</ul>", unsafe_allow_html=True)


def run_caption(ctx: DashContext) -> None:
    if ctx.run is None:
        st.caption("No completed run yet for this workspace.")
        return
    r = ctx.run
    st.caption(
        md(
            f"Latest run #{r.id} · {r.run_date} · {r.mode} · status {r.status} · LLM cost ${r.cost_usd:.2f}"
            + (f" · as of {r.as_of:%Y-%m-%d %H:%M} UTC" if r.as_of else "")
        )
    )
