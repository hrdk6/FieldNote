"""Shared test fixtures. Every test runs against temporary databases and output folders."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from fieldnote.config import apply_fixture_overlay, get_settings, load_workspace
from fieldnote.db.engine import Database, get_database, reset_database_cache
from fieldnote.llm.cache import LLMCacheStore
from fieldnote.llm.mock_client import MockClient

REPO = Path(__file__).resolve().parents[1]
ENV_KEYS = [
    "DATABASE_URL",
    "FIELDNOTE_OUT_DIR",
    "FIELDNOTE_DATA_DIR",
    "FIELDNOTE_LLM",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "NVIDIA_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "SMTP_HOST",
    "SMTP_FROM",
    "SMTP_TO",
    "REDDIT_CLIENT_ID",
    "REDDIT_CLIENT_SECRET",
    "YOUTUBE_API_KEY",
    "FIELDNOTE_WORKSPACES_DIR",
    "FIELDNOTE_FIXTURES_DIR",
]


def _isolate(mp: pytest.MonkeyPatch, root: Path) -> None:
    # Never let a developer's real .env (API keys) leak into tests.
    mp.setattr("fieldnote.config._load_dotenv_once", lambda: None)
    for k in ENV_KEYS:
        mp.delenv(k, raising=False)
    mp.setenv("DATABASE_URL", f"sqlite:///{(root / 'test.db').as_posix()}")
    mp.setenv("FIELDNOTE_OUT_DIR", str(root / "out"))
    mp.setenv("FIELDNOTE_DATA_DIR", str(root / "data"))
    mp.setenv("FIELDNOTE_LLM", "mock")
    mp.setenv("FIELDNOTE_WORKSPACES_DIR", str(REPO / "workspaces"))
    mp.setenv("FIELDNOTE_FIXTURES_DIR", str(REPO / "fixtures"))
    mp.setenv("FIELDNOTE_LOG_LEVEL", "WARNING")
    # Tests use fake hosts and fake sessions; keep them hermetic (no DNS lookups for the SSRF guard).
    mp.setenv("FIELDNOTE_ALLOW_PRIVATE_URLS", "1")
    for k in (
        "GEMINI_API_KEY",
        "NVIDIA_API_KEY",
        "GROQ_API_KEY",
        "FIELDNOTE_DASHBOARD_PASSWORD",
        "FIELDNOTE_DASHBOARD_READONLY",
        *(
            f"{p}_{v}"
            for p in ("GEMINI", "NVIDIA", "GROQ")
            for v in ("MODEL", "MODELS", "FAST_MODEL", "FAST_MODELS", "RPM")
        ),
    ):
        mp.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    _isolate(monkeypatch, tmp_path)
    yield tmp_path
    reset_database_cache()


@pytest.fixture
def db(isolated_env: Path) -> Database:
    return get_database(os.environ["DATABASE_URL"])


@pytest.fixture
def cfg_ev():  # type: ignore[no-untyped-def]
    return apply_fixture_overlay(load_workspace("ev_two_wheelers_india"))


@pytest.fixture
def cfg_phones():  # type: ignore[no-untyped-def]
    return apply_fixture_overlay(load_workspace("budget_smartphones_india"))


@pytest.fixture
def mock_llm(db: Database) -> MockClient:
    return MockClient(get_settings(), cache=LLMCacheStore(db, "test"))


@pytest.fixture(scope="session")
def offline_run(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict]:
    """One full offline pipeline run (both fixture rounds, weekly memo) shared by end-to-end style tests."""
    root = tmp_path_factory.mktemp("e2e")
    mp = pytest.MonkeyPatch()
    _isolate(mp, root)
    from fieldnote.pipeline import run_pipeline

    summary = run_pipeline(load_workspace("ev_two_wheelers_india"), offline=True, dry_run=True, weekly=True)
    db = get_database(os.environ["DATABASE_URL"])
    yield {
        "summary": summary,
        "db": db,
        "root": root,
        "mp": mp,
        "cfg": apply_fixture_overlay(load_workspace("ev_two_wheelers_india")),
    }
    mp.undo()
    reset_database_cache()


@pytest.fixture
def e2e(offline_run: dict, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Point the environment at the shared offline run for this test."""
    root = offline_run["root"]
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(root / 'test.db').as_posix()}")
    monkeypatch.setenv("FIELDNOTE_OUT_DIR", str(root / "out"))
    monkeypatch.setenv("FIELDNOTE_DATA_DIR", str(root / "data"))
    return offline_run
