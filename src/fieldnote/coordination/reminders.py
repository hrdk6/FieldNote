"""Reminders: a grouped digest of overdue / soon-due open items, at most once per item per day."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from fieldnote.config import Settings, WorkspaceConfig
from fieldnote.db import repo
from fieldnote.db.engine import Database, utcnow
from fieldnote.db.models import ActionItem
from fieldnote.delivery.dispatch import send_digest


@dataclass
class ReminderResult:
    overdue: list[dict[str, Any]] = field(default_factory=list)
    due_soon: list[dict[str, Any]] = field(default_factory=list)
    skipped_already_reminded: int = 0
    deliveries: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""


def _reminded_today(a: ActionItem, cfg: WorkspaceConfig, today: date) -> bool:
    return a.last_reminded_at is not None and cfg.local_date(a.last_reminded_at) == today


def build_digest(cfg: WorkspaceConfig, overdue: list[ActionItem], due_soon: list[ActionItem], today: date) -> str:
    lines = [f"FieldNote reminders: {cfg.display_name} ({today.isoformat()})", ""]

    def fmt(a: ActionItem) -> str:
        return f"- #{a.id} {a.description} (due {a.due_date.isoformat() if a.due_date else '-'}, {a.priority})"

    for title, items in (("Overdue", overdue), ("Due soon", due_soon)):
        if not items:
            continue
        lines.append(f"{title}:")
        by_owner: dict[str, list[ActionItem]] = {}
        for a in items:
            by_owner.setdefault(a.owner, []).append(a)
        for owner in sorted(by_owner):
            lines.append(f"  {owner}:")
            lines += [f"  {fmt(a)}" for a in by_owner[owner]]
        lines.append("")
    lines.append("Items are never auto-completed. Mark them done with `fieldnote actions done <id>`.")
    return "\n".join(lines)


def run_reminders(
    db: Database,
    cfg: WorkspaceConfig,
    settings: Settings,
    *,
    dry_run: bool,
    within_days: int | None = None,
    now: datetime | None = None,
) -> ReminderResult:
    now = now or utcnow()
    today = cfg.local_date(now)
    horizon = today + timedelta(days=cfg.reminders.due_within_days if within_days is None else within_days)
    res = ReminderResult()
    with db.session() as s:
        items = repo.items_due(s, cfg.workspace, horizon)
        fresh = []
        for a in items:
            if _reminded_today(a, cfg, today):
                res.skipped_already_reminded += 1
                continue
            fresh.append(a)
        overdue = [a for a in fresh if a.due_date and a.due_date < today]
        due_soon = [a for a in fresh if a.due_date and a.due_date >= today]
        res.overdue = [
            {"id": a.id, "description": a.description, "owner": a.owner, "due": a.due_date.isoformat()}
            for a in overdue
            if a.due_date
        ]
        res.due_soon = [
            {"id": a.id, "description": a.description, "owner": a.owner, "due": a.due_date.isoformat()}
            for a in due_soon
            if a.due_date
        ]
        if not fresh:
            return res
        res.text = build_digest(cfg, overdue, due_soon, today)
        res.deliveries = send_digest(
            cfg, settings, f"FieldNote reminders ({today.isoformat()})", res.text, dry_run=dry_run
        )
        if any(d["status"] in ("sent", "dry_run") for d in res.deliveries):
            for a in fresh:
                a.last_reminded_at = now
    return res
