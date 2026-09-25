"""Delivery routing. Outbound actions are triggered only by the CLI/scheduler, never by model output.

A channel is used only when it is BOTH listed in the workspace ``delivery.channels`` AND has
credentials in the environment. Otherwise (or with ``--dry-run``) output goes to the outbox.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fieldnote.config import Settings, WorkspaceConfig
from fieldnote.db import repo
from fieldnote.db.engine import Database, utcnow
from fieldnote.delivery.dryrun import write_outbox
from fieldnote.delivery.email import EmailError, send_email
from fieldnote.delivery.telegram import TelegramClient, TelegramError
from fieldnote.logging_setup import get_logger
from fieldnote.reports.daily_brief import telegram_chunks_from_file

log = get_logger(__name__)


def active_channels(cfg: WorkspaceConfig, settings: Settings, dry_run: bool) -> tuple[list[str], list[str]]:
    """(live channels, reasons for dry-run)."""
    if dry_run:
        return [], ["--dry-run requested"]
    live: list[str] = []
    reasons: list[str] = []
    for ch in cfg.delivery.channels:
        if (ch == "telegram" and settings.has_telegram) or (ch == "email" and settings.has_email):
            live.append(ch)
        else:
            reasons.append(f"{ch}: credentials not configured")
    if not cfg.delivery.channels:
        reasons.append("no delivery channels configured for this workspace")
    return live, reasons


def _send(
    channel: str,
    settings: Settings,
    subject: str,
    text: str,
    tg_chunks: list[str] | None,
    html: str | None,
    attachments: list[Path],
) -> None:
    if channel == "telegram":
        tg = TelegramClient(settings)
        tg.send_messages(tg_chunks or [text], parse_mode="MarkdownV2" if tg_chunks else None)
        for a in attachments:
            tg.send_document(a, caption=subject)
    elif channel == "email":
        send_email(settings, subject, text, html, attachments)


def deliver_briefs(
    db: Database,
    cfg: WorkspaceConfig,
    settings: Settings,
    run_id: int | None,
    *,
    dry_run: bool,
    kinds: tuple[str, ...] = ("daily", "weekly"),
) -> list[dict[str, Any]]:
    live, reasons = active_channels(cfg, settings, dry_run)
    results: list[dict[str, Any]] = []
    with db.session() as s:
        briefs = [
            b
            for b in repo.list_briefs(s, cfg.workspace, limit=20)
            if b.kind in kinds and (run_id is None or b.run_id == run_id)
        ]
        seen: set[str] = set()
        for b in briefs:
            if b.kind in seen:
                continue
            seen.add(b.kind)
            fm = b.formats or {}
            if b.kind == "daily":
                subject = f"FieldNote daily brief: {cfg.display_name} ({(b.meta or {}).get('date', '')})"
                text = Path(fm["md"]).read_text(encoding="utf-8") if fm.get("md") else ""
                html = Path(fm["html"]).read_text(encoding="utf-8") if fm.get("html") else None
                chunks = telegram_chunks_from_file(fm["txt"]) if fm.get("txt") else None
                attachments: list[Path] = []
            else:
                subject = f"FieldNote weekly memo: {cfg.display_name} ({(b.meta or {}).get('week_end', '')})"
                text = f"{subject}\n\nThe weekly decision memo is attached (PDF). AI-assisted; sources linked; estimates labeled."
                html, chunks = None, None
                attachments = [Path(fm["pdf"])] if fm.get("pdf") else []
            channels_used = []
            for ch in live:
                try:
                    _send(ch, settings, subject, text, chunks, html, attachments)
                    channels_used.append(ch)
                    results.append(
                        {"brief": b.kind, "channel": ch, "status": "sent", "message": f"{b.kind} sent via {ch}"}
                    )
                except (TelegramError, EmailError, OSError) as exc:
                    log.error("delivery via %s failed: %s", ch, exc)
                    results.append({"brief": b.kind, "channel": ch, "status": "failed", "message": str(exc)[:200]})
            if not live:
                body = "\n\n-----8<-----\n\n".join(chunks) if chunks else text
                path = write_outbox(cfg.workspace, "dryrun", subject, body, attachments)
                channels_used.append("dryrun")
                results.append(
                    {
                        "brief": b.kind,
                        "channel": "dryrun",
                        "status": "dry_run",
                        "message": f"{b.kind} written to {path} ({'; '.join(reasons)})",
                    }
                )
            if channels_used:
                b.delivered_at = utcnow()
                b.channel = ",".join(channels_used)
    return results


def send_digest(
    cfg: WorkspaceConfig, settings: Settings, subject: str, text: str, *, dry_run: bool
) -> list[dict[str, Any]]:
    """Plain-text digest (used by reminders)."""
    live, reasons = active_channels(cfg, settings, dry_run)
    out = []
    for ch in live:
        try:
            _send(ch, settings, subject, text, None, None, [])
            out.append({"channel": ch, "status": "sent", "message": f"sent via {ch}"})
        except (TelegramError, EmailError, OSError) as exc:
            out.append({"channel": ch, "status": "failed", "message": str(exc)[:200]})
    if not live:
        path = write_outbox(cfg.workspace, "dryrun", subject, text)
        out.append({"channel": "dryrun", "status": "dry_run", "message": f"written to {path} ({'; '.join(reasons)})"})
    return out
