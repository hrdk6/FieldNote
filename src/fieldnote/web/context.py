"""Which database the dashboard reads, and the per-request context the API pages share.

The dashboard shows live data only: the database in ``DATABASE_URL`` or the live SQLite file. The bundled
fixture dataset is shown only when the server was started with ``fieldnote dashboard --demo``, and then it
is labelled as such on every page.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache

from fieldnote.config import ConfigError, WorkspaceConfig, apply_fixture_overlay, resolve_database_url
from fieldnote.db import repo
from fieldnote.db.engine import Database, get_database, utcnow
from fieldnote.db.models import Run


@dataclass
class Ctx:
    cfg: WorkspaceConfig
    db: Database
    run: Run | None
    fixture: bool
    now: datetime

    @property
    def run_id(self) -> int | None:
        return self.run.id if self.run else None


def demo_mode() -> bool:
    return os.environ.get("FIELDNOTE_DASHBOARD_DEMO", "").strip().lower() in ("1", "true", "yes", "on")


def database_url(workspace: str | None = None) -> str:
    """``DATABASE_URL`` if set, else the live SQLite file (``--demo`` points ``DATABASE_URL`` at the fixtures)."""
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    return resolve_database_url(offline=False)


@lru_cache(maxsize=8)
def _db(url: str) -> Database:
    return get_database(url)


def db_for(workspace: str) -> Database:
    return _db(database_url(workspace))


def registry() -> Database:
    """Database holding workspace copies (the same database the pages read)."""
    return _db(resolve_database_url(offline=False))


def build(workspace: str) -> Ctx:
    from fieldnote.workspaces import load

    db = db_for(workspace)
    cfg = load(workspace, registry())
    with db.session() as s:
        run = repo.last_run(s, cfg.workspace, kinds=("daily",), statuses=("success", "partial", "failed"))
        if run is not None:
            s.expunge(run)
    fixture = demo_mode() and bool(run and (run.mode == "offline" or (run.stats or {}).get("fixture")))
    if fixture:
        try:
            cfg = apply_fixture_overlay(cfg)
        except ConfigError:
            fixture = False
    # Live data is read relative to the real present; the fixture dataset is frozen at its own date.
    now = run.as_of if fixture and run and run.as_of else utcnow()
    return Ctx(cfg=cfg, db=db, run=run, fixture=fixture, now=now)
