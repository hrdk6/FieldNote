from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from fieldnote.ask.answer import AskSession, ask
from fieldnote.cli import app
from fieldnote.config import get_settings
from fieldnote.db import repo
from fieldnote.domain import CollectedDocument
from fieldnote.llm.mock_client import MockClient

runner = CliRunner()
NOTE = Path(__file__).resolve().parents[1] / "fixtures" / "meeting_notes" / "note_01.md"


def test_ask_answers_with_citations_and_memory(e2e: dict) -> None:
    db, cfg = e2e["db"], e2e["cfg"]
    llm = MockClient(get_settings())
    now = e2e["summary"].as_of
    sess = AskSession()
    a = ask(db, cfg, "What changed in competitor pricing this week?", llm=llm, session=sess, now=now)
    assert a.sentences and all(snt.citations for snt in a.sentences)
    assert a.sources and all(src.url for src in a.sources)
    assert "₹1,14,999" in a.markdown
    b = ask(db, cfg, "What are customers complaining about Kestrel service?", llm=llm, session=sess, now=now)
    assert "Kestrel" in b.markdown and b.entities == ["Kestrel"]
    c = ask(db, cfg, "and what about their range?", llm=llm, session=sess, now=now)
    assert c.entities == ["Kestrel"], "follow-up inherits the previous entity"
    d = ask(db, cfg, "What is the Martian market share of teleporters?", llm=llm, now=now)
    assert d.missing and not d.sentences
    with db.session() as s:
        asks = repo.list_briefs(s, cfg.workspace, kind="ask")
        assert len(asks) >= 4 and asks[0].meta["question"]


def test_retention_purge(db, cfg_ev) -> None:  # type: ignore[no-untyped-def]
    now = datetime(2026, 9, 22)
    with db.session() as s:
        for i, (st, age) in enumerate([("reddit", 200), ("reddit", 10), ("news", 400), ("news", 100)]):
            repo.upsert_document(
                s,
                cfg_ev.workspace,
                CollectedDocument(
                    source_type=st,
                    source_name="x",
                    url=f"https://x.example/{i}",
                    title=f"t{i}",
                    content=f"c{i}",
                    published_at=now - timedelta(days=age),
                ),
            )
    with db.session() as s:
        res = repo.purge_documents(s, cfg_ev.workspace, now, posts_days=180, documents_days=365)
    assert res == {"posts": 1, "documents": 1}
    with db.session() as s:
        assert repo.count_documents(s, cfg_ev.workspace) == 2


def test_cli_smoke(isolated_env: Path) -> None:
    assert runner.invoke(app, ["version"]).exit_code == 0
    r = runner.invoke(app, ["doctor", "--no-network"])
    assert r.exit_code == 0, r.output
    assert "config ev_two_wheelers_india" in r.output
    r = runner.invoke(app, ["notes", "add", str(NOTE), "--offline", "--yes", "--date", "2026-09-16"])
    assert r.exit_code == 0, r.output
    assert "4 new items" in r.output
    r = runner.invoke(app, ["actions", "list", "--offline"])
    assert r.exit_code == 0 and "Priya" in r.output
    r = runner.invoke(app, ["actions", "done", "1", "--offline"])
    assert r.exit_code == 0 and "done" in r.output
    assert runner.invoke(app, ["actions", "done", "999", "--offline"]).exit_code == 1
    assert runner.invoke(app, ["remind", "--dry-run", "--offline"]).exit_code == 0
    assert runner.invoke(app, ["export", "--offline"]).exit_code == 0
    assert runner.invoke(app, ["purge", "--dry-run", "--offline"]).exit_code == 0
    assert runner.invoke(app, ["brief", "daily", "--offline"]).exit_code == 1  # no run yet: clear error
    r = runner.invoke(app, ["run", "--offline", "--dry-run", "-w", "budget_smartphones_india"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["brief", "weekly", "--offline", "-w", "budget_smartphones_india"])
    assert r.exit_code == 0 and "weekly_pdf" in r.output
    r = runner.invoke(app, ["ask", "Which action items are open?", "--offline"])
    assert r.exit_code == 0 and "Action #" in r.output
    r = runner.invoke(app, ["run", "-w", "no_such_workspace"])
    assert r.exit_code == 1 and "not found" in r.output
