"""Workspaces: create one for any topic in plain language, run it, edit it, import/export and archive."""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from fieldnote.builder.catalog import CATEGORIES, REGIONS, TEMPLATES, get_template, search
from fieldnote.builder.core import (
    BuilderError,
    BuildRequest,
    BuildResult,
    ReviewEdits,
    apply_edits,
    build_workspace,
    finalize,
    split_csv,
)
from fieldnote.config import ConfigError, get_settings
from fieldnote.costs import CostTracker
from fieldnote.dashboard import runner
from fieldnote.dashboard.components import (
    DashContext,
    can_edit,
    db_for,
    empty_state,
    md,
    registry,
    select_workspace,
)
from fieldnote.db import repo
from fieldnote.llm.base import make_llm
from fieldnote.llm.cache import LLMCacheStore
from fieldnote.workspaces import archive, get_yaml, list_entries, parse_yaml_text, restore, save

BUILD_KEY = "_fn_build"
LIB_PICK = "_fn_lib_pick"
OTHER_REGION = "Other region..."
FLASH_KEY = "_fn_flash"
REVIEW_PREFIX = "rv_"
MAX_IMPORT_BYTES = 200_000
EXAMPLES = [
    "Quick-commerce grocery delivery in India: Blinkit, Zepto, Swiggy Instamart, BigBasket",
    "UK challenger banks such as Monzo, Starling and Revolut",
    "Solid-state battery startups like QuantumScape, Solid Power and Factorial",
    "AI coding assistants: GitHub Copilot, Cursor, Windsurf, Tabnine",
]


def _cell(value: Any) -> str:
    """Text of an editable table cell; empty cells arrive as None or NaN."""
    if value is None or (isinstance(value, float) and value != value):
        return ""
    return str(value).strip()


def render(ctx: DashContext | None) -> None:
    flash = st.session_state.pop(FLASH_KEY, None)
    if flash:
        st.success(flash, icon=":material/check_circle:")
    tabs = st.tabs(["New workspace", "This workspace", "All workspaces"])
    with tabs[0]:
        _new_workspace()
    with tabs[1]:
        if ctx is None:
            empty_state("No workspace selected yet.", "Create one in the first tab.")
        else:
            _this_workspace(ctx)
    with tabs[2]:
        _all_workspaces()


# ---- new workspace --------------------------------------------------------------------------------


def _builder_llm() -> Any:
    return make_llm(get_settings(), cache=LLMCacheStore(registry(), "_builder"), cost=CostTracker(budget_usd=0.5))


def _pick_template(template_id: str, region: str) -> None:
    """Button callback: remember the choice and show its description in the form."""
    st.session_state[LIB_PICK] = (template_id, region)
    st.session_state["fn_desc"] = get_template(template_id).description(region)


def _library(llm: Any) -> None:
    c1, c2, c3 = st.columns([3, 2, 2])
    query = c1.text_input("Search", placeholder="e.g. payments, airlines, Zomato, solar", key="fn_lib_q")
    region_choice = c2.selectbox("Region", [*REGIONS, OTHER_REGION], key="fn_lib_region")
    region = (
        c3.text_input("Country or region", placeholder="e.g. Germany, Brazil", key="fn_lib_region_other").strip()
        if region_choice == OTHER_REGION
        else str(region_choice)
    )
    category = st.pills("Category", ["All", *CATEGORIES], default="All", key="fn_lib_cat")
    matches = search(query, None if category in (None, "All") else str(category))
    if not matches:
        st.caption("No template matches. Describe your own topic below.")
        return
    st.caption(
        f"{len(matches)} of {len(TEMPLATES)} topics. Choosing one drafts a workspace for {region or 'your region'}; "
        "the model can add players and sources, and every source is verified before you save."
    )
    for row_start in range(0, len(matches), 3):
        # One row of columns at a time keeps reading order on narrow screens, where columns stack.
        cols = st.columns(3)
        for col, t in zip(cols, matches[row_start : row_start + 3], strict=False):
            _template_card(col, t, region, llm)


def _template_card(col: Any, t: Any, region: str, llm: Any) -> None:
    with col.container(border=True):
        st.markdown(f"**{md(t.title)}**")
        st.caption(md(f"{t.category} · {t.blurb}"))
        players = t.players_for(region)
        if players:
            st.caption(md("Players: " + ", ".join(players)))
        elif llm.is_mock:
            st.caption("No example players for this region; add an LLM key or name them in the form below.")
        else:
            st.caption("Players: chosen by the model for this region.")
        st.button(
            "Use this",
            key=f"fn_lib_use_{t.id}",
            on_click=_pick_template,
            args=(t.id, region),
            disabled=not region,
            width="stretch",
        )


def _new_workspace() -> None:
    if not can_edit():
        st.info("This dashboard is read-only (FIELDNOTE_DASHBOARD_READONLY). Use `fieldnote workspace new` instead.")
        return
    llm = _builder_llm()
    st.markdown(
        "Pick a topic from the library or describe any market, industry, set of companies, technology or topic. "
        "FieldNote drafts the entities, customer-voice aspects and news searches, then checks every proposed "
        "source before you save."
    )
    st.caption(f"Drafting with: {'rule-based drafter (no LLM key set)' if llm.is_mock else llm.name}.")
    has_draft = isinstance(st.session_state.get(BUILD_KEY), BuildResult)
    with st.expander(
        f"Workspace library: {len(TEMPLATES)} topics in {len(CATEGORIES)} categories",
        expanded=not has_draft,
        icon=":material/grid_view:",
    ):
        _library(llm)
    st.markdown("**Or describe your own**")
    st.caption("Examples: " + " · ".join(f"“{e}”" for e in EXAMPLES[:2]))
    with st.form("fn_builder"):
        description = st.text_area(
            "What do you want to track?", height=110, max_chars=3000, placeholder=EXAMPLES[0], key="fn_desc"
        )
        with st.expander("Options"):
            c1, c2 = st.columns(2)
            region = c1.text_input("Region", placeholder="inferred from the description, e.g. India or Global")
            language = c2.text_input("Language code", placeholder="en", max_chars=5)
            perspective = c1.text_input("Perspective", placeholder="market-level observer")
            focal = c2.text_input("Focal company (optional)", placeholder="the company briefs are written for")
            verify = st.checkbox("Verify sources now (recommended; takes up to a minute)", value=True)
        submitted = st.form_submit_button("Draft workspace", type="primary", icon=":material/auto_awesome:")
    pick = st.session_state.pop(LIB_PICK, None)
    req: BuildRequest | None = None
    if submitted:
        req = BuildRequest(description, region or None, perspective or None, focal or None, None, language or None)
    elif pick:
        template_id, pick_region = pick
        req = BuildRequest(get_template(template_id).description(pick_region), pick_region)
    if req is not None:
        taken = {e.name for e in list_entries(registry(), include_archived=True)}
        with st.status("Building your workspace...", expanded=True) as status:
            try:
                result = build_workspace(
                    req, llm=llm, settings=get_settings(), taken=taken, verify=verify, progress=status.write
                )
            except BuilderError as exc:
                status.update(label="Could not build a workspace", state="error")
                st.error(str(exc))
                return
            finally:
                llm.cache.flush()
            status.update(label="Draft ready: review it below", state="complete", expanded=False)
        for key in [k for k in st.session_state if str(k).startswith(REVIEW_PREFIX)]:
            del st.session_state[key]
        st.session_state[BUILD_KEY] = result
    result = st.session_state.get(BUILD_KEY)
    if isinstance(result, BuildResult):
        _review(result)


def _review(result: BuildResult) -> None:
    cfg = result.config
    counts = result.counts()
    st.divider()
    st.subheader("Review before saving")
    st.caption(
        f"Draft by {result.drafted_by} · sources: {counts['ok']} verified, {counts['dropped']} dropped, "
        f"{counts['unverified']} unverified"
    )
    for w in result.warnings:
        st.warning(md(w), icon=":material/warning:")
    c1, c2 = st.columns(2)
    display = c1.text_input("Name", value=cfg["display_name"], key=f"{REVIEW_PREFIX}display", max_chars=80)
    slug = c2.text_input(
        "Workspace id",
        value=cfg["workspace"],
        key=f"{REVIEW_PREFIX}slug",
        help="Lowercase letters, digits, underscores.",
    )
    perspective = c1.text_input("Perspective", value=cfg["perspective"], key=f"{REVIEW_PREFIX}persp", max_chars=200)
    focal = c2.text_input("Focal company", value=cfg["focal_company"] or "", key=f"{REVIEW_PREFIX}focal")
    st.caption(f"Region: {cfg['region']} · timezone {cfg['timezone']} · languages {', '.join(cfg['language_hints'])}")

    st.markdown("**Companies, brands or technologies**")
    original_names = [c["name"] for c in cfg["competitors"]]
    ent_df = pd.DataFrame(
        [
            {"name": c["name"], "aliases": ", ".join(c["aliases"]), "tracked pages": len(c["pages"])}
            for c in cfg["competitors"]
        ]
    )
    ent_edit = st.data_editor(
        ent_df,
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        disabled=["tracked pages"],
        column_config={
            "name": st.column_config.TextColumn("name", required=True, max_chars=60),
            "aliases": st.column_config.TextColumn("aliases (comma-separated)", width="large"),
        },
        key=f"{REVIEW_PREFIX}entities",
    )
    st.markdown("**Customer-voice aspects**")
    asp_df = pd.DataFrame(
        [{"aspect": a, "signal words": ", ".join(cfg["aspect_keywords"].get(a, []))} for a in cfg["aspects"]]
    )
    asp_edit = st.data_editor(
        asp_df,
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        column_config={"signal words": st.column_config.TextColumn("signal words (comma-separated)", width="large")},
        key=f"{REVIEW_PREFIX}aspects",
    )
    news = cfg["sources"]["news"]
    st.markdown("**Sources**")
    queries = st.text_area(
        "News search phrases (one per line; searched through the open GDELT news index)",
        value="\n".join(news["search_queries"]),
        height=120,
        key=f"{REVIEW_PREFIX}queries",
    )
    feeds = st.multiselect(
        "Verified RSS/Atom feeds", news["rss_feeds"], default=news["rss_feeds"], key=f"{REVIEW_PREFIX}feeds"
    )
    page_labels = {p["url"]: f"{c['name']} · {p['kind']} · {p['url']}" for c in cfg["competitors"] for p in c["pages"]}
    pages = st.multiselect(
        "Verified pages to watch for changes",
        list(page_labels),
        default=list(page_labels),
        format_func=lambda u: page_labels.get(u, u),
        key=f"{REVIEW_PREFIX}pages",
    )
    subs = cfg["sources"]["reddit"]["subreddits"]
    subreddits = st.multiselect("Subreddits", subs, default=subs, key=f"{REVIEW_PREFIX}subs")
    c3, c4 = st.columns(2)
    reddit_terms = c3.text_input(
        "Reddit search terms (comma-separated)",
        value=", ".join(cfg["sources"]["reddit"]["search_terms"]),
        key=f"{REVIEW_PREFIX}rterms",
    )
    youtube_terms = c4.text_input(
        "YouTube search terms (comma-separated)",
        value=", ".join(cfg["sources"]["youtube"]["search_terms"]),
        key=f"{REVIEW_PREFIX}yterms",
    )
    if result.checks:
        with st.expander(f"Source checks ({len(result.checks)})"):
            st.dataframe(
                pd.DataFrame(
                    [{"kind": c.kind, "status": c.status, "source": c.value, "detail": c.detail} for c in result.checks]
                ),
                hide_index=True,
                width="stretch",
            )

    entities: list[tuple[str, list[str], int | None]] = []
    for _, row in ent_edit.iterrows():
        name = _cell(row.get("name"))
        if not name:
            continue
        idx = original_names.index(name) if name in original_names else None
        entities.append((name, split_csv(row.get("aliases")), idx))
    aspects = [
        (_cell(row.get("aspect")), split_csv(row.get("signal words")))
        for _, row in asp_edit.iterrows()
        if _cell(row.get("aspect"))
    ]
    edits = ReviewEdits(
        display_name=display,
        workspace=slug,
        perspective=perspective,
        focal_company=focal,
        entities=entities,
        aspects=aspects,
        search_queries=[q for q in queries.splitlines() if q.strip()],
        feeds=feeds,
        pages=pages,
        subreddits=subreddits,
        reddit_terms=split_csv(reddit_terms),
        youtube_terms=split_csv(youtube_terms),
    )
    error = ""
    yaml_text = ""
    try:
        edited = apply_edits(cfg, edits)
        _cfg, yaml_text = finalize(
            edited,
            description=result.request.description,
            checks=result.checks,
            drafted_by=result.drafted_by,
            when=result.created_at,
        )
    except BuilderError as exc:
        error = str(exc)
    taken = {e.name for e in list_entries(registry(), include_archived=True)}
    if not error and slug.strip() in taken:
        error = f"A workspace with id '{slug.strip()}' already exists; choose another id."
    if error:
        st.error(f"Fix before saving: {error}", icon=":material/error:")
    else:
        with st.expander("YAML preview"):
            st.code(yaml_text, language="yaml")
    run_after = st.checkbox(
        "Start the first collection right after saving (delivery stays in the dry-run outbox)",
        value=True,
        key=f"{REVIEW_PREFIX}runafter",
    )
    b1, b2 = st.columns([1, 1])
    create = b1.button("Create workspace", type="primary", disabled=bool(error), icon=":material/add:")
    if b2.button("Discard draft"):
        st.session_state.pop(BUILD_KEY, None)
        st.rerun()
    if create:
        try:
            saved = save(yaml_text, origin="builder", description=result.request.description, db=registry())
        except ConfigError as exc:
            st.error(str(exc))
            return
        msg = f"Created '{saved.name}'."
        if saved.file_error:
            msg += " Stored in the database only (the workspaces folder is not writable)."
        if run_after:
            try:
                runner.launch_run(db_for(saved.name), saved.name)
                msg += " The first live collection is running: follow it under 'This workspace'."
            except (runner.RunBusyError, ConfigError, OSError) as exc:
                msg += f" Could not start the first run ({exc}); start it under 'This workspace'."
        st.session_state.pop(BUILD_KEY, None)
        st.session_state[FLASH_KEY] = msg
        select_workspace(saved.name)
        st.rerun()


# ---- this workspace -----------------------------------------------------------------------------


def _this_workspace(ctx: DashContext) -> None:
    ws = ctx.cfg.workspace
    try:
        text, source = get_yaml(ws, registry())
    except ConfigError as exc:
        st.error(str(exc))
        return
    st.markdown(f"#### {md(ctx.cfg.display_name)}")
    news = ctx.cfg.sources.news
    entry = next((e for e in list_entries(registry(), include_archived=True) if e.name == ws), None)
    stored = " and ".join(
        x
        for x, ok in ((f"workspaces/{ws}.yaml", entry and entry.has_file), ("the database", entry and entry.has_row))
        if ok
    )
    st.caption(
        f"id `{ws}` · saved in {stored or source} (active copy: {source}) · {len(ctx.cfg.competitors)} entities · "
        f"{len(news.search_queries)} news searches · {len(news.rss_feeds)} feeds · "
        f"{sum(len(c.pages) for c in ctx.cfg.competitors)} watched pages"
    )
    _run_panel(ctx)
    st.divider()
    _edit_panel(ctx, text)


def _run_panel(ctx: DashContext) -> None:
    ws = ctx.cfg.workspace
    busy = runner.is_busy(ctx.db, ws)
    c1, c2 = st.columns([1, 3])
    clicked = c1.button(
        "Run now", type="primary", icon=":material/play_arrow:", disabled=busy or not can_edit(), key="fn_run_now"
    )
    c2.caption(
        "Collects live sources, runs the agents and writes a fresh brief. Delivery from here always goes to the "
        "dry-run outbox; the scheduler sends. Free LLM tiers are rate limited, so a run can take several minutes."
    )
    if clicked:
        try:
            runner.launch_run(ctx.db, ws)
            st.toast("Run started")
            st.rerun()
        except (runner.RunBusyError, ConfigError, OSError) as exc:
            st.error(str(exc))

    @st.fragment(run_every="5s" if busy else None)
    def progress() -> None:
        now_busy = runner.is_busy(ctx.db, ws)
        if st.session_state.get("_fn_was_busy") and not now_busy:
            st.session_state["_fn_was_busy"] = False
            st.rerun()
        st.session_state["_fn_was_busy"] = now_busy
        active = runner.active_run(ctx.db, ws)
        if now_busy:
            label = f"run #{active.id} started {active.started_at:%H:%M} UTC" if active else "starting"
            st.info(f"A run is in progress ({label}). This panel refreshes every 5 seconds.", icon=":material/sync:")
        with ctx.db.session() as s:
            runs = repo.list_runs(s, ws, limit=8)
            rows = [
                {
                    "run": r.id,
                    "date": r.run_date,
                    "mode": r.mode,
                    "status": r.status,
                    "started (UTC)": f"{r.started_at:%Y-%m-%d %H:%M}" if r.started_at else "",
                    "minutes": f"{(r.finished_at - r.started_at).total_seconds() / 60:.1f}"
                    if r.finished_at and r.started_at
                    else "",
                    "new documents": str((r.stats or {}).get("documents_new", "")),
                    "LLM cost $": round(r.cost_usd or 0.0, 4),
                }
                for r in runs
            ]
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        else:
            st.caption("No runs yet.")
        path, log = runner.latest_log(ws)
        if path is not None:
            with st.expander(f"Latest dashboard run log ({path.name})", expanded=now_busy):
                st.code(log or "(empty)", language="text")

    progress()


def _edit_panel(ctx: DashContext, text: str) -> None:
    ws = ctx.cfg.workspace
    st.markdown("**Configuration**")
    st.download_button("Download YAML", text.encode("utf-8"), file_name=f"{ws}.yaml", mime="application/x-yaml")
    if not can_edit():
        st.code(text, language="yaml")
        return
    edited = st.text_area(
        "Edit the workspace YAML (validated before saving; the id cannot change here)",
        value=text,
        height=420,
        key=f"fn_yaml_{ws}_{hash(text)}",
    )
    if st.button("Save changes", icon=":material/save:", disabled=edited == text):
        try:
            cfg = parse_yaml_text(edited, f"edit:{ws}")
            if cfg.workspace != ws:
                raise ConfigError(f"the id must stay '{ws}'; to rename, create a new workspace and archive this one")
            saved = save(edited, origin="edit", db=registry(), overwrite=True)
        except ConfigError as exc:
            st.error(str(exc))
            return
        note = "" if saved.file_path else " (database only: the workspaces folder is not writable)"
        st.session_state[FLASH_KEY] = f"Saved '{ws}'{note}. Run `fieldnote doctor -w {ws}` to re-check new URLs."
        st.rerun()


# ---- all workspaces --------------------------------------------------------------------------------


def _all_workspaces() -> None:
    db = registry()
    entries = list_entries(db, include_archived=True)
    rows = []
    for e in entries:
        with db_for(e.name).session() as s:
            run = repo.last_run(s, e.name, kinds=("daily",), statuses=("success", "partial", "failed"))
        rows.append(
            {
                "id": e.name,
                "name": e.display_name,
                "stored in": e.storage,
                "active copy": e.source,
                "status": e.status if e.valid else "invalid",
                "last run": f"{run.run_date} ({run.status})" if run else "",
                "problem": e.error.splitlines()[0][:160] if e.error else "",
            }
        )
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    else:
        empty_state("No workspaces yet.", "Create one in the first tab.")
    if not can_edit():
        return
    active = [e.name for e in entries if e.status == "active"]
    archived = [e.name for e in entries if e.status == "archived"]
    c1, c2 = st.columns(2)
    with c1, st.container(border=True):
        st.markdown("**Archive**")
        st.caption("Hides a workspace from lists and scheduled runs. Collected data is kept; restore any time.")
        target = st.selectbox("Workspace to archive", active, index=None, key="fn_arch_target")
        sure = st.checkbox("Yes, archive it", key="fn_arch_sure")
        if st.button("Archive", disabled=not (target and sure)):
            try:
                archive(str(target), db)
            except ConfigError as exc:
                st.error(str(exc))
            else:
                st.session_state[FLASH_KEY] = f"Archived '{target}'."
                st.rerun()
    with c2, st.container(border=True):
        st.markdown("**Restore**")
        st.caption("Brings an archived workspace back into lists and scheduled runs.")
        back = st.selectbox("Archived workspace", archived, index=None, key="fn_restore_target")
        if st.button("Restore", disabled=not back):
            try:
                restore(str(back), db)
            except ConfigError as exc:
                st.error(str(exc))
            else:
                st.session_state[FLASH_KEY] = f"Restored '{back}'."
                select_workspace(str(back))
                st.rerun()
    with st.container(border=True):
        st.markdown("**Import a workspace YAML**")
        up = st.file_uploader("Workspace file", type=["yaml", "yml"], key="fn_import_file")
        overwrite = st.checkbox("Replace an existing workspace with the same id", key="fn_import_over")
        if st.button("Import", disabled=up is None) and up is not None:
            data = up.getvalue()
            if len(data) > MAX_IMPORT_BYTES:
                st.error("That file is too large for a workspace config (limit 200 KB).")
                return
            try:
                saved = save(data.decode("utf-8"), origin="import", db=db, overwrite=overwrite)
            except (ConfigError, UnicodeDecodeError) as exc:
                st.error(str(exc))
                return
            st.session_state[FLASH_KEY] = (
                f"Imported '{saved.name}'. Run `fieldnote doctor -w {saved.name}` to check its URLs."
            )
            select_workspace(saved.name)
            st.rerun()
