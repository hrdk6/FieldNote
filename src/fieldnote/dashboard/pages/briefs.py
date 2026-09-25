"""Briefs: list, preview, download (MD/HTML/PDF) and send now (dry-run unless delivery is configured)."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from fieldnote.config import get_settings
from fieldnote.dashboard.components import DashContext, empty_state, md
from fieldnote.db import repo
from fieldnote.delivery.dispatch import active_channels, deliver_briefs


def render(ctx: DashContext) -> None:
    cfg = ctx.cfg
    kind = st.segmented_control("Type", ["daily", "weekly", "ask"], default="daily")
    with ctx.db.session() as s:
        rows = [
            {
                "id": b.id,
                "kind": b.kind,
                "run_id": b.run_id,
                "path": b.path,
                "formats": b.formats or {},
                "meta": b.meta or {},
                "created": b.created_at,
                "delivered": b.delivered_at,
                "channel": b.channel,
            }
            for b in repo.list_briefs(s, cfg.workspace, kind=kind, limit=60)
        ]
    if not rows:
        empty_state(f"No {kind} briefs yet.", "Briefs are written by `fieldnote run` and `fieldnote brief weekly`.")
        return
    labels = {
        r["id"]: f"#{r['id']} · {r['created']:%Y-%m-%d %H:%M}"
        + (f" · {r['meta'].get('question', '')[:60]}" if kind == "ask" else f" · run {r['run_id']}")
        + (f" · delivered via {r['channel']}" if r["delivered"] else "")
        for r in rows
    }
    sel = st.selectbox("Brief", [r["id"] for r in rows], format_func=lambda i: labels[i])
    b = next(r for r in rows if r["id"] == sel)
    fm = b["formats"]
    cols = st.columns(4)
    for i, (fmt, mime) in enumerate(
        (("md", "text/markdown"), ("html", "text/html"), ("pdf", "application/pdf"), ("txt", "text/plain"))
    ):
        p = Path(fm[fmt]) if fm.get(fmt) else None
        if p and p.exists():
            cols[i].download_button(
                f"Download {fmt.upper()}", p.read_bytes(), file_name=p.name, mime=mime, width="stretch"
            )
    if kind in ("daily", "weekly"):
        live, reasons = active_channels(cfg, get_settings(), dry_run=False)
        where = ", ".join(live) if live else "the dry-run outbox (" + "; ".join(reasons) + ")"
        with st.popover("Send now", icon=":material/send:"):
            st.markdown(f"This will deliver the latest {kind} brief to **{where}**.")
            if st.button("Confirm send", type="primary"):
                res = deliver_briefs(
                    ctx.db,
                    cfg,
                    get_settings(),
                    b["run_id"] if kind == "daily" else None,
                    dry_run=not live,
                    kinds=(kind,),
                )
                for r in res:
                    (st.success if r["status"] in ("sent", "dry_run") else st.error)(
                        f"{r['channel']}: {r['status']} - {r['message']}"
                    )
    md_path = Path(fm.get("md", b["path"]))
    if md_path.suffix == ".md" and md_path.exists():
        st.markdown("---")
        st.markdown(md(md_path.read_text(encoding="utf-8")), unsafe_allow_html=False)
    elif fm.get("pdf"):
        st.info("This brief is a PDF; use the download button above.")
