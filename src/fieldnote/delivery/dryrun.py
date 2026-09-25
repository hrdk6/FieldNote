"""Dry-run delivery: the default whenever credentials are missing. Writes to out/<workspace>/outbox/."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from fieldnote.config import workspace_out_dir
from fieldnote.textutil import slugify


def write_outbox(
    workspace: str,
    channel: str,
    subject: str,
    body: str,
    attachments: list[Path] | None = None,
    now: datetime | None = None,
) -> Path:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    folder = workspace_out_dir(workspace) / "outbox"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stamp}_{channel}_{slugify(subject, 50)}.txt"
    lines = [f"CHANNEL: {channel} (dry run - nothing was sent)", f"SUBJECT: {subject}"]
    for a in attachments or []:
        lines.append(f"ATTACHMENT: {a.name}")
    path.write_text("\n".join(lines) + "\n\n" + body, encoding="utf-8")
    for a in attachments or []:
        if a.exists():
            shutil.copy2(a, folder / a.name)
    return path
