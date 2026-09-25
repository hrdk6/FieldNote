"""Database engine and session management.

SQLite by default, Postgres-compatible via ``DATABASE_URL``. Schema management is a simple versioned
``create_all`` plus a ``schema_version`` table (see docs/design-decisions.md).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from fieldnote.db.models import SCHEMA_VERSION, Base, SchemaVersion
from fieldnote.logging_setup import get_logger

log = get_logger(__name__)


def utcnow() -> datetime:
    """Naive UTC timestamp. All stored timestamps are naive UTC for SQLite/Postgres parity."""
    return datetime.now(UTC).replace(tzinfo=None)


class Database:
    def __init__(self, url: str, echo: bool = False) -> None:
        self.url = url
        if url.startswith("sqlite:///") and ":memory:" not in url:
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine: Engine = create_engine(url, echo=echo, future=True, connect_args=connect_args)
        if url.startswith("sqlite"):
            event.listen(self.engine, "connect", _sqlite_pragmas)
        self._sessionmaker = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        self.ensure_schema()

    def ensure_schema(self) -> None:
        Base.metadata.create_all(self.engine)
        _add_missing_columns(self.engine)
        with self.session() as s:
            current = s.scalar(select(SchemaVersion).order_by(SchemaVersion.version.desc()).limit(1))
            if current is None or current.version < SCHEMA_VERSION:
                s.add(SchemaVersion(version=SCHEMA_VERSION, applied_at=utcnow()))

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self._sessionmaker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def dispose(self) -> None:
        self.engine.dispose()


def _sqlite_pragmas(dbapi_conn, _record) -> None:  # type: ignore[no-untyped-def]
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


def _add_missing_columns(engine: Engine) -> None:
    """Lightweight forward migration: add nullable columns that exist in models but not in the DB."""
    insp = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing:
                continue
            coltype = col.type.compile(dialect=engine.dialect)
            log.info("migrating: adding column %s.%s", table.name, col.name)
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}'))


_DB_CACHE: dict[str, Database] = {}


def get_database(url: str) -> Database:
    db = _DB_CACHE.get(url)
    if db is None:
        db = Database(url)
        _DB_CACHE[url] = db
    return db


def reset_database_cache() -> None:
    for db in _DB_CACHE.values():
        db.dispose()
    _DB_CACHE.clear()
