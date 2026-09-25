"""End-to-end: `fieldnote run --offline` on fixtures with the MockClient, then the eval thresholds."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select

from fieldnote.config import get_settings, load_workspace
from fieldnote.db import repo
from fieldnote.db.models import ChangeEvent, Claim, Document, Finding, Run, Theme
from fieldnote.evals.run_evals import run_evals
from fieldnote.pipeline import run_pipeline


def test_offline_run_produces_briefs_pdf_and_rows(e2e: dict) -> None:
    s = e2e["summary"]
    assert s.status == "success" and s.exit_code == 0
    md = Path(s.briefs["daily_md"])
    pdf = Path(s.briefs["weekly_pdf"])
    assert md.exists() and "FIXTURE DATA" in md.read_text(encoding="utf-8")
    assert pdf.exists() and pdf.read_bytes()[:4] == b"%PDF" and pdf.stat().st_size > 20_000
    assert Path(s.briefs["daily_html"]).exists() and Path(s.briefs["daily_txt"]).exists()
    text = md.read_text(encoding="utf-8")
    for section in (
        "## Top findings",
        "## What changed",
        "## Customer voice",
        "## Metric movers",
        "## Actions due",
        "Verification:",
    ):
        assert section in text
    assert "[Fact]" in text and "[Analysis]" in text and "[Estimate" in text
    ws = e2e["cfg"].workspace
    with e2e["db"].session() as sess:
        assert sess.scalar(select(func.count(Document.id)).where(Document.workspace == ws)) > 80
        assert sess.scalar(select(func.count(ChangeEvent.id)).where(ChangeEvent.workspace == ws)) == 4
        assert sess.scalar(select(func.count(Theme.id)).where(Theme.workspace == ws)) >= 3
        verified = sess.scalar(select(func.count(Claim.id)).where(Claim.workspace == ws, Claim.status == "verified"))
        assert verified >= 20
        assert sess.scalar(select(func.count(Finding.id)).where(Finding.workspace == ws)) >= 5
        runs = list(sess.scalars(select(Run).where(Run.workspace == ws)))
        assert {r.kind for r in runs} == {"baseline", "daily"}
        brief = repo.latest_brief(sess, ws, "daily")
        assert brief is not None and brief.delivered_at is not None and brief.channel == "dryrun"


def test_rejected_claims_never_appear_in_outputs(e2e: dict) -> None:
    s = e2e["summary"]
    outputs = Path(s.briefs["daily_md"]).read_text(encoding="utf-8") + Path(s.briefs["weekly_md"]).read_text(
        encoding="utf-8"
    )
    with e2e["db"].session() as sess:
        for c in repo.all_claims(sess, e2e["cfg"].workspace):
            if c.status != "verified" and len(c.text) > 30:
                assert c.text not in outputs


def test_eval_thresholds_are_met(e2e: dict) -> None:
    report = run_evals(e2e["db"], e2e["cfg"], get_settings(), write_report=True)
    assert report.passed, report.checks
    m = report.metrics
    assert m["citation_validity"]["rate"] >= 0.95
    assert m["adversarial"]["rate"] >= 0.90
    assert m["number_consistency"]["rate"] >= 0.99
    assert m["extraction"]["precision"] >= 0.8 and m["extraction"]["recall"] >= 0.8
    assert m["ranking"]["deterministic"] and m["ranking"]["weight_sensitivity"]
    assert (Path(e2e["root"]) / "out" / "eval_report.md").exists()


def test_same_day_rerun_updates_instead_of_duplicating(e2e: dict) -> None:
    ws = e2e["cfg"].workspace
    with e2e["db"].session() as sess:
        before = {
            "docs": sess.scalar(select(func.count(Document.id)).where(Document.workspace == ws)),
            "runs": sess.scalar(select(func.count(Run.id)).where(Run.workspace == ws)),
            "changes": sess.scalar(select(func.count(ChangeEvent.id)).where(ChangeEvent.workspace == ws)),
            "findings": sess.scalar(select(func.count(Finding.id)).where(Finding.workspace == ws)),
        }
        urls_before = set(sess.scalars(select(Document.url).where(Document.workspace == ws)))
    again = run_pipeline(load_workspace("ev_two_wheelers_india"), offline=True, dry_run=True, weekly=False)
    assert again.status == "success"
    with e2e["db"].session() as sess:
        after = {
            "docs": sess.scalar(select(func.count(Document.id)).where(Document.workspace == ws)),
            "runs": sess.scalar(select(func.count(Run.id)).where(Run.workspace == ws)),
            "changes": sess.scalar(select(func.count(ChangeEvent.id)).where(ChangeEvent.workspace == ws)),
            "findings": sess.scalar(select(func.count(Finding.id)).where(Finding.workspace == ws)),
        }
        urls_after = set(sess.scalars(select(Document.url).where(Document.workspace == ws)))
    assert urls_after == urls_before, urls_after ^ urls_before
    assert after == before


def test_second_workspace_runs_with_config_only(tmp_path: Path) -> None:
    s = run_pipeline(load_workspace("budget_smartphones_india"), offline=True, dry_run=True)
    assert s.status == "success"
    text = Path(s.briefs["daily_md"]).read_text(encoding="utf-8")
    assert "Budget smartphones" in text and "hours" in text
