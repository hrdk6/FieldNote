"""Action tracker operations: list, update, done/drop, CSV export. Items are never auto-completed."""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import Any

from fieldnote.config import WorkspaceConfig, workspace_out_dir
from fieldnote.db import repo
from fieldnote.db.engine import Database
from fieldnote.db.models import ActionItem

STATUSES = ("open", "done", "dropped")
PRIORITIES = ("low", "medium", "high")
CSV_FIELDS = [
    "id",
    "description",
    "owner",
    "due_date",
    "priority",
    "status",
    "confidence",
    "flags",
    "source_sentence",
    "created_at",
]


class TrackerError(Exception):
    pass


def item_dict(a: ActionItem) -> dict[str, Any]:
    return {
        "id": a.id,
        "description": a.description,
        "owner": a.owner,
        "due_date": a.due_date.isoformat() if a.due_date else "",
        "priority": a.priority,
        "status": a.status,
        "confidence": round(a.confidence, 2),
        "flags": ", ".join(a.flags or []),
        "source_sentence": a.source_sentence,
        "created_at": a.created_at.strftime("%Y-%m-%d %H:%M") if a.created_at else "",
        "last_reminded_at": a.last_reminded_at.strftime("%Y-%m-%d %H:%M") if a.last_reminded_at else "",
    }


def list_items(
    db: Database, cfg: WorkspaceConfig, statuses: tuple[str, ...] | None = ("open",)
) -> list[dict[str, Any]]:
    with db.session() as s:
        return [item_dict(a) for a in repo.action_items(s, cfg.workspace, statuses)]


def update_item(
    db: Database,
    cfg: WorkspaceConfig,
    item_id: int,
    *,
    status: str | None = None,
    owner: str | None = None,
    due_date: date | None = None,
    clear_due: bool = False,
    priority: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    with db.session() as s:
        a = repo.get_action_item(s, cfg.workspace, item_id)
        if a is None:
            raise TrackerError(f"action item {item_id} not found in workspace {cfg.workspace}")
        if status is not None:
            if status not in STATUSES:
                raise TrackerError(f"status must be one of {STATUSES}")
            a.status = status
        if owner is not None:
            a.owner = owner.strip() or "Unassigned"
            a.flags = [f for f in (a.flags or []) if f not in ("missing_owner", "ambiguous_owner", "owner_not_in_text")]
        if clear_due:
            a.due_date = None
        elif due_date is not None:
            a.due_date = due_date
        if priority is not None:
            if priority not in PRIORITIES:
                raise TrackerError(f"priority must be one of {PRIORITIES}")
            a.priority = priority
        if description is not None and description.strip():
            a.description = description.strip()
        return item_dict(a)


def export_csv(
    db: Database, cfg: WorkspaceConfig, path: Path | None = None, statuses: tuple[str, ...] | None = None
) -> Path:
    path = path or (workspace_out_dir(cfg.workspace) / "action_items.csv")
    with db.session() as s:
        rows = [item_dict(a) for a in repo.action_items(s, cfg.workspace, statuses)]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path
