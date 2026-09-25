"""Start pipeline runs as background processes and report their progress.

A run is the ordinary CLI so it behaves exactly like one started by hand:

* ``analysis``: ``fieldnote run -w <id> --dry-run`` (collect, process, agents, briefs; delivery to the
  outbox). The live scheduler passes ``deliver=True`` once a day to send the brief for real.
* ``pulse``: ``fieldnote pulse -w <id>`` (collect and process only), the frequent live refresh.

A second run of the same workspace is refused while one is running, whether it was started from this
process (tracked by process handle) or elsewhere (a ``running`` row in the database).
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from fieldnote.config import SLUG_RE, ConfigError, workspace_out_dir
from fieldnote.db.engine import Database, utcnow
from fieldnote.db.models import Run

STALE_RUN_HOURS = 3
KEEP_LOGS = 40
_SRC_DIR = Path(__file__).resolve().parents[2]
# Processes started by this dashboard process (module state survives Streamlit reruns).
_PROCS: dict[str, subprocess.Popen[bytes]] = {}
_LOCK = threading.Lock()


class RunBusyError(Exception):
    """A run for this workspace is already in progress."""


@dataclass
class LaunchInfo:
    log_path: Path
    pid: int


def _logs_dir(workspace: str) -> Path:
    path = workspace_out_dir(workspace) / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def active_run(db: Database, workspace: str, now: datetime | None = None) -> Run | None:
    """The run currently marked ``running`` (ignoring ones older than a few hours, which crashed)."""
    now = now or utcnow()
    with db.session() as s:
        run = s.scalar(
            select(Run)
            .where(Run.workspace == workspace, Run.status == "running")
            .where(Run.started_at >= now - timedelta(hours=STALE_RUN_HOURS))
            .order_by(Run.id.desc())
            .limit(1)
        )
        if run is not None:
            s.expunge(run)
        return run


def started_here(workspace: str) -> bool:
    """True while a run launched from this dashboard process is still alive."""
    with _LOCK:
        proc = _PROCS.get(workspace)
        if proc is None:
            return False
        if proc.poll() is None:
            return True
        del _PROCS[workspace]
        return False


def is_busy(db: Database, workspace: str) -> bool:
    return started_here(workspace) or active_run(db, workspace) is not None


def _prune_logs(workspace: str) -> None:
    files = sorted(_logs_dir(workspace).glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[KEEP_LOGS:]:
        with contextlib.suppress(OSError):
            old.unlink()


def launch_run(db: Database, workspace: str, kind: str = "analysis", deliver: bool = False) -> LaunchInfo:
    """Start an ``analysis`` run (``fieldnote run``) or a ``pulse`` (``fieldnote pulse``) in the background."""
    if not SLUG_RE.match(workspace):
        raise ConfigError(f"invalid workspace name {workspace!r}")
    if kind not in ("analysis", "pulse"):
        raise ConfigError(f"unknown run kind {kind!r}")
    with _LOCK:
        running = _PROCS.get(workspace)
        if (running is not None and running.poll() is None) or active_run(db, workspace) is not None:
            raise RunBusyError(f"a run for '{workspace}' is already in progress")
        _prune_logs(workspace)
        prefix = "pulse" if kind == "pulse" else "dashboard-run"
        log_path = _logs_dir(workspace) / f"{prefix}-{datetime.now():%Y%m%d-%H%M%S}.log"
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONPATH"] = os.pathsep.join(p for p in (str(_SRC_DIR), env.get("PYTHONPATH", "")) if p)
        if kind == "pulse":
            cmd = [sys.executable, "-m", "fieldnote.cli", "pulse", "-w", workspace]
        else:
            cmd = [sys.executable, "-m", "fieldnote.cli", "run", "-w", workspace, *([] if deliver else ["--dry-run"])]
        with open(log_path, "wb") as log:
            if os.name == "nt":
                flags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                proc = subprocess.Popen(
                    cmd,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    cwd=Path.cwd(),
                    env=env,
                    creationflags=flags,
                )
            else:
                proc = subprocess.Popen(
                    cmd,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    cwd=Path.cwd(),
                    env=env,
                    start_new_session=True,
                )
        _PROCS[workspace] = proc
    return LaunchInfo(log_path=log_path, pid=proc.pid)


def running_procs() -> dict[str, int]:
    """Workspaces with a run process started from this process that is still alive (name -> pid)."""
    with _LOCK:
        return {ws: p.pid for ws, p in _PROCS.items() if p.poll() is None}


def latest_log(workspace: str, max_chars: int = 6000) -> tuple[Path | None, str]:
    """The most recent run log for a workspace (tail), analysis or pulse."""
    files = sorted(_logs_dir(workspace).glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None, ""
    text = files[0].read_text(encoding="utf-8", errors="replace")
    return files[0], text[-max_chars:]
