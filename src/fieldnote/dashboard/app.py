"""FieldNote dashboard (Streamlit). Run with `fieldnote dashboard` or `streamlit run src/fieldnote/dashboard/app.py`."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import streamlit as st

# Allow `streamlit run path/to/app.py` from a source checkout without installing the package.
_SRC = Path(__file__).resolve().parents[2]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from fieldnote.dashboard import components as c  # noqa: E402
from fieldnote.dashboard.pages import (  # noqa: E402
    actions,
    ask,
    board,
    briefs,
    evals,
    inspector,
    market,
    metrics,
    overview,
    voice,
    workspaces,
)
from fieldnote.dashboard.theme import inject_css  # noqa: E402

st.set_page_config(page_title="FieldNote", page_icon=":material/insights:", layout="wide")
inject_css()
if not c.require_login():
    st.stop()

st.sidebar.markdown("### FieldNote")
st.sidebar.caption("Configurable AI chief-of-staff")
workspace = c.workspace_switcher()
ctx = c.build_context(workspace) if workspace else None
if ctx is not None:
    st.sidebar.caption(f"Database: `{c.database_url(workspace).split('@')[-1].split('/')[-1]}`")
    if ctx.fixture:
        st.sidebar.warning("Fixture data (demo)", icon=":material/science:")

PAGES: list[tuple[str, str, Callable[[Any], None]]] = [
    ("Overview", ":material/dashboard:", overview.render),
    ("Market & competitors", ":material/storefront:", market.render),
    ("Metrics", ":material/monitoring:", metrics.render),
    ("Customer voice", ":material/forum:", voice.render),
    ("Opportunity Board", ":material/leaderboard:", board.render),
    ("Ask FieldNote", ":material/chat:", ask.render),
    ("Briefs", ":material/description:", briefs.render),
    ("Actions", ":material/checklist:", actions.render),
    ("Run Inspector", ":material/manage_search:", inspector.render),
    ("Evals", ":material/verified:", evals.render),
]


def _page(title: str, fn: Callable[[Any], None], banner: bool = True) -> Callable[[], None]:
    def runner() -> None:
        st.title(title)
        if ctx is not None and banner:
            c.fixture_banner(ctx)
        fn(ctx)

    runner.__name__ = "page_" + title.lower().replace(" ", "_").replace("&", "and")
    return runner


def _st_page(title: str, icon: str, fn: Callable[[Any], None], default: bool = False, banner: bool = True) -> Any:
    run = _page(title, fn, banner)
    return st.Page(run, title=title, icon=icon, url_path=run.__name__[5:], default=default)


workspaces_page = _st_page("Workspaces", ":material/tune:", workspaces.render, default=ctx is None, banner=False)
if ctx is None:
    st.sidebar.info("No workspace yet. Describe what you want to track to create one.", icon=":material/add_circle:")
    nav = st.navigation([workspaces_page])
else:
    pages = [_st_page(t, i, f, default=n == 0) for n, (t, i, f) in enumerate(PAGES)]
    nav = st.navigation({"Insights": pages, "Setup": [workspaces_page]})
    if c.can_edit() and st.sidebar.button("New workspace", icon=":material/add:", width="stretch"):
        st.switch_page(workspaces_page)
    if st.session_state.pop(c.OPEN_LIBRARY, False):
        st.switch_page(workspaces_page)
nav.run()
