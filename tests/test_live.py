"""Live mode: pulse runs, the scheduler's clock and choices, the live-only dashboard context, change watermarks."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select

from fieldnote.db import repo
from fieldnote.db.engine import Database, utcnow
from fieldnote.db.models import Claim, Document, Run
from fieldnote.live.scheduler import LiveConfig, LiveScheduler, schedule
from fieldnote.pipeline import PULSE_STAGES, Pipeline, RunOptions

CFG = LiveConfig(enabled=True, pulse_minutes=30, analysis_hours=6, deliver_hour=7, max_concurrent=2)


def _run(db: Database, ws: str, kind: str, status: str, finished: datetime | None, mode: str = "live") -> int:
    with db.session() as s:
        r = Run(
            workspace=ws,
            run_date=(finished or utcnow()).date().isoformat(),
            mode=mode,
            kind=kind,
            status=status,
            as_of=finished,
            started_at=(finished or utcnow()) - timedelta(minutes=3),
            finished_at=finished,
        )
        s.add(r)
        s.flush()
        return r.id


def test_pulses_continue_one_run_a_day_and_leave_the_analysis_alone(db: Database, cfg_ev: Any) -> None:
    ws = cfg_ev.workspace
    as_of = datetime(2026, 9, 22, 6, 0)
    Pipeline(cfg_ev, RunOptions(offline=True, dry_run=True, round_label="current", as_of=as_of), db=db).run()
    with db.session() as s:
        daily = repo.last_run(s, ws, kinds=("daily",), statuses=("success", "partial"))
        assert daily is not None
        claims_before = s.scalar(select(func.count(Claim.id)).where(Claim.run_id == daily.id))
    for i in (1, 2):
        opts = RunOptions(
            offline=True,
            dry_run=True,
            stages=PULSE_STAGES,
            round_label="current",
            as_of=as_of + timedelta(minutes=30 * i),
            kind="pulse",
        )
        summary = Pipeline(cfg_ev, opts, db=db).run()
        assert summary.status == "success"
        assert [st.name for st in summary.stages if st.status != "skipped"] == ["collect", "process"]
    with db.session() as s:
        pulses = list(s.scalars(select(Run).where(Run.workspace == ws, Run.kind == "pulse")))
        assert len(pulses) == 1  # runs are unique per day and kind: later pulses continue the row
        assert pulses[0].stats["pulses_today"] == 2
        assert pulses[0].stats["since"].startswith("2026-09-22T06:30")  # collects since the previous pulse
        assert pulses[0].status == "success" and pulses[0].finished_at is not None
        assert s.scalar(select(func.count(Claim.id)).where(Claim.run_id == daily.id)) == claims_before
        again = repo.last_run(s, ws, kinds=("daily",), statuses=("success", "partial"))
        assert again is not None and again.id == daily.id  # the pages still read findings from the analysis


def test_same_day_analysis_rerun_keeps_its_window(db: Database) -> None:
    ws = "ev_two_wheelers_india"
    yesterday = utcnow() - timedelta(days=1)
    today_run = utcnow() - timedelta(hours=6)
    _run(db, ws, "daily", "success", yesterday)
    rid = _run(db, ws, "daily", "success", today_run)
    with db.session() as s:
        same = repo.find_run(s, ws, today_run.date().isoformat(), "live", "daily")
        assert same is not None and same.id == rid
        prev = repo.last_run(s, ws, kinds=("daily",), mode="live", before_as_of=utcnow(), exclude_id=same.id)
        assert prev is not None and prev.as_of == yesterday


def test_schedule_clock(db: Database) -> None:
    ws = "ev_two_wheelers_india"
    now = utcnow()
    first = schedule(db, ws, CFG, now)
    assert not first["has_analysis"] and first["next_pulse"] == now and first["next_analysis"] == now
    _run(db, ws, "pulse", "success", now - timedelta(minutes=10))
    _run(db, ws, "daily", "failed", now - timedelta(hours=1))
    sched = schedule(db, ws, CFG, now)
    # The pulse is the latest collection; a failed analysis retries after half its interval.
    assert sched["next_pulse"] == now - timedelta(minutes=10) + timedelta(minutes=30)
    assert sched["next_analysis"] == now - timedelta(hours=1) + timedelta(hours=3)
    assert not sched["has_analysis"]


def test_tick_starts_first_analysis_and_pulses_meanwhile(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldnote.dashboard import runner

    started: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(runner, "running_procs", lambda: {})
    monkeypatch.setattr(runner, "active_run", lambda _db, _ws, now=None: None)
    monkeypatch.setattr(
        runner, "launch_run", lambda _db, ws, kind="analysis", deliver=False: started.append((ws, kind, deliver))
    )
    sched = LiveScheduler(CFG)
    monkeypatch.setattr(sched, "_maybe_housekeeping", lambda *a: None)
    sched.tick()
    kinds = [k for _, k, _ in started]
    assert kinds == ["analysis", "pulse"]  # one analysis at a time; the next workspace gets a pulse meanwhile
    assert all(not d for _, _, d in started)  # no delivery channels configured in tests


def test_dashboard_reads_live_data_unless_demo(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldnote.web import context as wc

    ws = "ev_two_wheelers_india"
    fixed = datetime(2026, 9, 22, 6, 0)
    with db.session() as s:
        s.add(Run(workspace=ws, run_date="2026-09-22", mode="offline", kind="daily", status="success", as_of=fixed))
    wc._db.cache_clear()
    live = wc.build(ws)
    assert not live.fixture and live.now > fixed + timedelta(days=1)
    monkeypatch.setenv("FIELDNOTE_DASHBOARD_DEMO", "1")
    demo = wc.build(ws)
    assert demo.fixture and demo.now == fixed


def test_fingerprint_moves_when_data_arrives(db: Database) -> None:
    from fieldnote.web import context as wc
    from fieldnote.web.live_api import _fingerprint

    wc._db.cache_clear()
    ws = "ev_two_wheelers_india"
    before = _fingerprint(db, ws)
    with db.session() as s:
        s.add(
            Document(
                workspace=ws,
                source_type="news",
                source_name="example.com",
                url="https://example.com/a",
                title="A",
                content="text",
                content_hash="h1",
            )
        )
    after = _fingerprint(db, ws)
    assert after["version"] != before["version"] and after["documents"] > before["documents"]
    assert after["active"] is None
