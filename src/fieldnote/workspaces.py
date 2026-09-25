"""Workspace registry: YAML files in ``workspaces/`` plus copies stored in the database.

Files are the primary format for local use (readable, diffable, easy to commit). Copies in the
``workspace_configs`` table let a hosted dashboard create or edit workspaces that a scheduler on
another machine (GitHub Actions, a scheduler container) then picks up through the shared
``DATABASE_URL``.

Resolution when both copies exist: each database row records the SHA-256 of the file version it
superseded (``base_file_sha``). If the file on disk still has that content, the row wins (a dashboard
edit is newer than the file). If the file changed since, the file wins (a local edit or a newer
commit is newer than the row). Timestamps are deliberately not used: a git checkout resets file
modification times.

Archiving hides a workspace from lists and scheduled runs and keeps its collected data. The file (if
any) moves to ``workspaces/_archived/`` and the row is marked ``archived``.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from sqlalchemy import select

from fieldnote.config import (
    SLUG_RE,
    ConfigError,
    WorkspaceConfig,
    load_workspace_file,
    parse_workspace,
    resolve_database_url,
    safe_join,
    workspaces_dir,
)
from fieldnote.db.engine import Database, get_database, utcnow
from fieldnote.db.models import WorkspaceConfigRow
from fieldnote.logging_setup import get_logger

log = get_logger(__name__)

ARCHIVE_DIR = "_archived"
Origin = Literal["file", "database"]


@dataclass
class WorkspaceEntry:
    name: str
    display_name: str
    source: Origin
    status: Literal["active", "archived"]
    valid: bool
    error: str = ""
    has_file: bool = False
    has_row: bool = False
    updated_at: datetime | None = None
    description: str = ""

    @property
    def storage(self) -> str:
        """Where copies exist: 'file', 'database' or 'file + database'."""
        return " + ".join(x for x, ok in (("file", self.has_file), ("database", self.has_row)) if ok) or self.source


@dataclass
class SaveResult:
    name: str
    file_path: Path | None
    file_error: str | None


def registry_db() -> Database:
    """The database that stores workspace copies: ``DATABASE_URL`` or the live SQLite file."""
    return get_database(resolve_database_url(offline=False))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _check_slug(name: str) -> None:
    if not SLUG_RE.match(name):
        raise ConfigError(f"invalid workspace name {name!r}")


def _file_path(name: str) -> Path:
    return safe_join(workspaces_dir(), f"{name}.yaml")


def _archived_path(name: str) -> Path:
    return safe_join(workspaces_dir(), ARCHIVE_DIR, f"{name}.yaml")


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def parse_yaml_text(text: str, source: str) -> WorkspaceConfig:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source}: YAML syntax error: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: expected a mapping at the top level")
    return parse_workspace(data, source)


def _rows(db: Database) -> dict[str, WorkspaceConfigRow]:
    with db.session() as s:
        rows = list(s.scalars(select(WorkspaceConfigRow)))
        for r in rows:
            s.expunge(r)
    return {r.workspace: r for r in rows}


def _row_wins(row: WorkspaceConfigRow | None, file_text: str | None) -> bool:
    if row is None:
        return False
    if file_text is None:
        return True
    return row.base_file_sha is not None and row.base_file_sha == _sha(file_text)


def list_entries(db: Database | None = None, *, include_archived: bool = False) -> list[WorkspaceEntry]:
    db = db or registry_db()
    rows = _rows(db)
    d = workspaces_dir()
    files = {p.stem: p for p in d.glob("*.yaml") if not p.stem.startswith("_")} if d.exists() else {}
    archived_files = {p.stem: p for p in (d / ARCHIVE_DIR).glob("*.yaml")} if (d / ARCHIVE_DIR).exists() else {}
    out: list[WorkspaceEntry] = []
    for name in sorted(set(files) | set(rows) | set(archived_files)):
        if not SLUG_RE.match(name):
            continue
        row = rows.get(name)
        file_text = _read(files[name]) if name in files else None
        if _row_wins(row, file_text):
            assert row is not None
            text, source, status = row.yaml_text, "database", row.status
        elif file_text is not None:
            text, source, status = file_text, "file", "active"
        else:
            # Only an archived file exists.
            text, source, status = _read(archived_files[name]) or "", "file", "archived"
        if status != "active" and not include_archived:
            continue
        entry = WorkspaceEntry(
            name=name,
            display_name=name,
            source=source,  # type: ignore[arg-type]
            status="archived" if status != "active" else "active",
            valid=True,
            has_file=name in files,
            has_row=row is not None,
            updated_at=row.updated_at if row is not None else None,
            description=row.description if row is not None else "",
        )
        try:
            cfg = parse_yaml_text(text, f"{source}:{name}")
            entry.display_name = cfg.display_name
            if cfg.workspace != name:
                raise ConfigError(f"'workspace: {cfg.workspace}' does not match the name '{name}'")
        except ConfigError as exc:
            entry.valid = False
            entry.error = str(exc)
        out.append(entry)
    return out


def list_names(db: Database | None = None) -> list[str]:
    """Active workspace slugs (valid or not; loading an invalid one raises ConfigError)."""
    return [e.name for e in list_entries(db)]


def get_yaml(name: str, db: Database | None = None) -> tuple[str, Origin]:
    """The effective YAML text of an active workspace and where it came from."""
    _check_slug(name)
    db = db or registry_db()
    row = _rows(db).get(name)
    path = _file_path(name)
    file_text = _read(path) if path.exists() else None
    if _row_wins(row, file_text):
        assert row is not None
        if row.status != "active":
            raise ConfigError(f"workspace '{name}' is archived; restore it first")
        return row.yaml_text, "database"
    if file_text is not None:
        return file_text, "file"
    available = ", ".join(list_names(db)) or "(none)"
    raise ConfigError(f"workspace '{name}' not found in {workspaces_dir()} or the database. Available: {available}")


def load(name_or_path: str | None = None, db: Database | None = None) -> WorkspaceConfig:
    """Load a workspace by slug (file or database copy) or by explicit YAML path."""
    from fieldnote.config import get_settings

    name = name_or_path or get_settings().default_workspace
    if name.endswith((".yaml", ".yml")) or os.sep in name or "/" in name:
        return load_workspace_file(Path(name).expanduser().resolve())
    text, source = get_yaml(name, db)
    if source == "file":
        return load_workspace_file(_file_path(name))
    cfg = parse_yaml_text(text, f"database:{name}")
    if cfg.workspace != name:
        raise ConfigError(f"database:{name}: 'workspace: {cfg.workspace}' does not match the name '{name}'")
    return cfg


def exists(name: str, db: Database | None = None) -> bool:
    """True if the slug is taken by an active or archived workspace (file or database)."""
    _check_slug(name)
    db = db or registry_db()
    return name in _rows(db) or _file_path(name).exists() or _archived_path(name).exists()


def save(
    yaml_text: str,
    *,
    origin: str,
    description: str = "",
    db: Database | None = None,
    overwrite: bool = False,
    write_file: bool = True,
) -> SaveResult:
    """Validate and store a workspace in the database and (when possible) as workspaces/<slug>.yaml."""
    cfg = parse_yaml_text(yaml_text, "<new workspace>")
    name = cfg.workspace
    _check_slug(name)
    db = db or registry_db()
    if not overwrite and exists(name, db):
        raise ConfigError(f"a workspace named '{name}' already exists; choose another name")
    path = _file_path(name)
    file_error: str | None = None
    written: Path | None = None
    if write_file:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".yaml.tmp")
            tmp.write_text(yaml_text, encoding="utf-8")
            tmp.replace(path)
            written = path
        except OSError as exc:
            file_error = f"could not write {path}: {exc.strerror or exc}"
            log.warning("%s; the workspace is stored in the database only", file_error)
    current_file = _read(path) if path.exists() else None
    with db.session() as s:
        row = s.scalar(select(WorkspaceConfigRow).where(WorkspaceConfigRow.workspace == name))
        if row is None:
            row = WorkspaceConfigRow(workspace=name, created_at=utcnow())
            s.add(row)
        row.yaml_text = yaml_text
        row.display_name = cfg.display_name[:200]
        row.origin = origin[:32]
        if description:
            row.description = description[:4000]
        row.status = "active"
        row.base_file_sha = _sha(current_file) if current_file is not None else None
        row.updated_at = utcnow()
    return SaveResult(name=name, file_path=written, file_error=file_error)


def archive(name: str, db: Database | None = None) -> None:
    """Hide a workspace from lists and scheduled runs. Collected data is kept."""
    _check_slug(name)
    db = db or registry_db()
    text, _source = get_yaml(name, db)
    path = _file_path(name)
    if path.exists():
        try:
            target = _archived_path(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            path.replace(target)
        except OSError as exc:
            log.warning("could not move %s to the archive folder (%s); archiving in the database only", path, exc)
    current_file = _read(path) if path.exists() else None
    with db.session() as s:
        row = s.scalar(select(WorkspaceConfigRow).where(WorkspaceConfigRow.workspace == name))
        if row is None:
            row = WorkspaceConfigRow(workspace=name, yaml_text=text, origin="file", created_at=utcnow())
            s.add(row)
            try:
                row.display_name = parse_yaml_text(text, name).display_name[:200]
            except ConfigError:
                row.display_name = name
        row.status = "archived"
        row.base_file_sha = _sha(current_file) if current_file is not None else None
        row.updated_at = utcnow()


def restore(name: str, db: Database | None = None) -> None:
    """Bring an archived workspace back."""
    _check_slug(name)
    db = db or registry_db()
    path = _file_path(name)
    archived = _archived_path(name)
    if archived.exists() and not path.exists():
        try:
            archived.replace(path)
        except OSError as exc:
            log.warning("could not move %s back (%s); restoring from the database copy", archived, exc)
    current_file = _read(path) if path.exists() else None
    with db.session() as s:
        row = s.scalar(select(WorkspaceConfigRow).where(WorkspaceConfigRow.workspace == name))
        if row is None:
            if current_file is None:
                raise ConfigError(f"workspace '{name}' is not archived")
            return
        row.status = "active"
        if current_file is not None and row.yaml_text != current_file:
            # The restored file is the latest version; let it win.
            row.base_file_sha = None
        else:
            row.base_file_sha = _sha(current_file) if current_file is not None else None
        row.updated_at = utcnow()
