"""Actions: editable tracker, paste-notes box with extraction preview and confirm, CSV export, reminders."""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from fieldnote.config import get_settings
from fieldnote.coordination.extractor import commit_preview, preview_note
from fieldnote.coordination.reminders import run_reminders
from fieldnote.coordination.tracker import PRIORITIES, STATUSES, TrackerError, export_csv, list_items, update_item
from fieldnote.costs import CostTracker
from fieldnote.dashboard.components import DashContext, empty_state, md
from fieldnote.llm.base import make_llm
from fieldnote.llm.cache import LLMCacheStore


def _tracker(ctx: DashContext) -> None:
    show = st.segmented_control("Status", [*STATUSES, "all"], default="open", key="act_status")
    rows = list_items(ctx.db, ctx.cfg, None if show == "all" else (show,))
    if not rows:
        empty_state("No action items here.", "Paste meeting notes below to extract some.")
        return
    df = pd.DataFrame(rows)
    df["due_date"] = pd.to_datetime(df["due_date"].replace("", None)).dt.date
    edited = st.data_editor(
        df[["id", "description", "owner", "due_date", "priority", "status", "flags", "source_sentence"]],
        hide_index=True,
        width="stretch",
        disabled=["id", "flags", "source_sentence"],
        column_config={
            "status": st.column_config.SelectboxColumn("status", options=list(STATUSES)),
            "priority": st.column_config.SelectboxColumn("priority", options=list(PRIORITIES)),
            "due_date": st.column_config.DateColumn("due date"),
            "description": st.column_config.TextColumn("description", width="large"),
        },
        key=f"editor_{ctx.cfg.workspace}_{show}",
    )
    changes = 0
    for _, row in edited.iterrows():
        orig = df[df["id"] == row["id"]].iloc[0]
        kwargs = {}
        for field in ("description", "owner", "priority", "status"):
            if row[field] != orig[field]:
                kwargs[field] = row[field]
        new_due = row["due_date"] if not pd.isna(row["due_date"]) else None
        old_due = orig["due_date"] if not pd.isna(orig["due_date"]) else None
        if new_due != old_due:
            kwargs["due_date" if new_due else "clear_due"] = new_due if new_due else True
        if kwargs:
            try:
                update_item(ctx.db, ctx.cfg, int(row["id"]), **kwargs)
                changes += 1
            except TrackerError as exc:
                st.error(str(exc))
    if changes:
        st.toast(f"Saved {changes} change(s)")
    c1, c2 = st.columns(2)
    path = export_csv(ctx.db, ctx.cfg)
    c1.download_button("Export CSV", path.read_bytes(), file_name=path.name, mime="text/csv")
    if c2.button("Run reminders (dry run unless delivery is configured)"):
        res = run_reminders(ctx.db, ctx.cfg, get_settings(), dry_run=False)
        if res.text:
            st.code(res.text)
            for d in res.deliveries:
                st.caption(md(f"{d['channel']}: {d['status']} - {d['message']}"))
        else:
            st.info(f"Nothing to remind ({res.skipped_already_reminded} already reminded today).")


def _paste_notes(ctx: DashContext) -> None:
    st.subheader("Add meeting notes")
    with st.form("notes_form"):
        title = st.text_input("Title", placeholder="Weekly market sync")
        note_date = st.date_input("Meeting date", value=ctx.cfg.local_date(ctx.now))
        body = st.text_area(
            "Paste notes", height=200, placeholder="Attendees: ...\n- Priya will prepare ... by Friday."
        )
        submitted = st.form_submit_button("Extract action items")
    if submitted and body.strip():
        llm = make_llm(
            get_settings(),
            offline=ctx.fixture,
            cache=LLMCacheStore(ctx.db, ctx.cfg.workspace),
            cost=CostTracker(budget_usd=0.5),
        )
        st.session_state["notes_preview"] = preview_note(
            ctx.db, ctx.cfg, llm, body, note_date if isinstance(note_date, date) else date.today(), title
        )
    prev = st.session_state.get("notes_preview")
    if prev is None:
        return
    st.markdown(
        f"**Preview** ({prev.via}): {len(prev.items)} item(s). Relative dates were resolved against {prev.note_date}."
    )
    df = pd.DataFrame(
        [
            {
                "keep": True,
                "description": i.description,
                "owner": i.owner,
                "due": i.due_date,
                "priority": i.priority,
                "confidence": round(i.confidence, 2),
                "flags": ", ".join(i.flags),
            }
            for i in prev.items
        ]
    )
    edited = st.data_editor(
        df,
        hide_index=True,
        width="stretch",
        disabled=["description", "owner", "due", "priority", "confidence", "flags"],
        key="preview_editor",
    )
    merge_ok = True
    if prev.merges:
        st.warning(
            "Some items look like existing open items: "
            + "; ".join(f"item {m.index} ~ #{m.existing_id} ({m.score:.0f}%)" for m in prev.merges)
        )
        merge_ok = st.checkbox("Merge them into the existing items instead of creating duplicates", value=True)
    if st.button("Confirm and save", type="primary"):
        keep = [i for i, k in enumerate(edited["keep"].tolist()) if k]
        res = commit_preview(ctx.db, ctx.cfg, prev, accept_merges=merge_ok, selected=keep)
        st.success(f"Saved: {len(res['created'])} new, {len(res['merged'])} merged.")
        st.session_state.pop("notes_preview", None)


def render(ctx: DashContext) -> None:
    _tracker(ctx)
    st.markdown("---")
    _paste_notes(ctx)
