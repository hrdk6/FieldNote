"""The live scheduler: frequent pulses, periodic analysis, one real delivery a day, daily housekeeping.

For every active, valid workspace it keeps two clocks:

* **pulse** (every ``FIELDNOTE_PULSE_MINUTES``, default 30): collect news, competitor pages and social posts,
  detect page changes and tag what is new. No agents, a handful of fast-model calls at most.
* **analysis** (every ``FIELDNOTE_ANALYSIS_HOURS``, default 6): the full pipeline, whose verified findings
  and brief replace the previous ones. A workspace with no analysis yet gets one immediately.

Once a day, after ``FIELDNOTE_DELIVER_AT_HOUR`` (default 7, workspace-local time), the next analysis is
run without ``--dry-run`` so the brief is sent through the workspace's configured channels (only when some
are configured); reminders, retention and (on Mondays) the weekly memo follow. Every run is an ordinary
CLI process started through :mod:`fieldnote.dashboard.runner`, so a crash never takes the server down and
a workspace never runs twice at once (a ``running`` row in the database blocks a second start, even from
another process or host).

The schedule is derived from the database each tick, not from memory, so a restart resumes exactly where
it left off and several processes can read the same state. Run it inside the web server (the default for
``fieldnote dashboard``) or on its own with ``fieldnote live``.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from fieldnote.config import ConfigError, _env, get_settings
from fieldnote.db import repo
from fieldnote.db.engine import Database, utcnow
from fieldnote.db.models import Brief, Run
from fieldnote.logging_setup import get_logger

log = get_logger(__name__)

TICK_SECONDS = 20


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    raw = _env(name)
    try:
        value = int(raw) if raw else default
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    return max(lo, min(hi, value))


@dataclass(frozen=True)
class LiveConfig:
    enabled: bool
    pulse_minutes: int
    analysis_hours: int
    deliver_hour: int
    max_concurrent: int

    @classmethod
    def from_env(cls) -> LiveConfig:
        get_settings()  # loads .env once
        return cls(
            enabled=(_env("FIELDNOTE_SCHEDULER", "on") or "on").strip().lower() not in ("0", "off", "false", "no"),
            pulse_minutes=_int_env("FIELDNOTE_PULSE_MINUTES", 30, 5, 24 * 60),
            analysis_hours=_int_env("FIELDNOTE_ANALYSIS_HOURS", 6, 1, 7 * 24),
            deliver_hour=_int_env("FIELDNOTE_DELIVER_AT_HOUR", 7, 0, 23),
            max_concurrent=_int_env("FIELDNOTE_MAX_CONCURRENT_RUNS", 2, 1, 8),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "pulse_minutes": self.pulse_minutes,
            "analysis_hours": self.analysis_hours,
            "deliver_hour": self.deliver_hour,
        }


# ---- schedule, derived from the database -------------------------------------------------------------


def _last(s: Any, workspace: str, kinds: tuple[str, ...]) -> Run | None:
    """The latest finished live run of these kinds (any outcome), by finish time."""
    return s.scalar(
        select(Run)
        .where(Run.workspace == workspace, Run.mode == "live", Run.kind.in_(kinds), Run.finished_at.is_not(None))
        .order_by(Run.finished_at.desc())
        .limit(1)
    )


def schedule(db: Database, workspace: str, cfg: LiveConfig, now: datetime | None = None) -> dict[str, Any]:
    """When this workspace last refreshed and when it is next due (UTC, naive)."""
    now = now or utcnow()
    with db.session() as s:
        pulse = _last(s, workspace, ("pulse", "daily"))
        analysis = _last(s, workspace, ("daily",))
        ok_analysis = repo.last_run(s, workspace, kinds=("daily",), mode="live")
        for r in {id(x): x for x in (pulse, analysis) if x is not None}.values():
            s.expunge(r)
        has_analysis = ok_analysis is not None

    # A failed run still moves the clock (no hammering a broken source), but only by half the interval.
    def nxt(r: Run | None, every: timedelta) -> datetime:
        if r is None or r.finished_at is None:
            return now
        step = every if r.status in ("success", "partial") else every / 2
        return r.finished_at + step

    return {
        "last_pulse": pulse,
        "last_analysis": analysis,
        "has_analysis": has_analysis,
        "next_pulse": nxt(pulse, timedelta(minutes=cfg.pulse_minutes)),
        "next_analysis": nxt(analysis, timedelta(hours=cfg.analysis_hours)),
    }


def _local_today(cfg_ws: Any, now: datetime) -> Any:
    return cfg_ws.local_date(now)


def _local_hour(cfg_ws: Any, now: datetime) -> int:
    from datetime import UTC
    from zoneinfo import ZoneInfo

    try:
        return now.replace(tzinfo=UTC).astimezone(ZoneInfo(cfg_ws.timezone)).hour
    except Exception:  # unknown timezone: fall back to UTC
        return now.hour


def delivery_due(db: Database, cfg_ws: Any, live: LiveConfig, now: datetime) -> bool:
    """True when today's brief should go out for real and has not yet (only if channels are configured)."""
    from fieldnote.delivery.dispatch import active_channels

    channels, _ = active_channels(cfg_ws, get_settings(), dry_run=False)
    if not channels or _local_hour(cfg_ws, now) < live.deliver_hour:
        return False
    today = _local_today(cfg_ws, now)
    with db.session() as s:
        rows = s.scalars(
            select(Brief)
            .where(Brief.workspace == cfg_ws.workspace, Brief.kind == "daily", Brief.delivered_at.is_not(None))
            .order_by(Brief.delivered_at.desc())
            .limit(5)
        )
        for b in rows:
            real = {c for c in (b.channel or "").split(",") if c and c != "dryrun"}
            if real and b.delivered_at and cfg_ws.local_date(b.delivered_at) == today:
                return False
    return True


# ---- the scheduler -------------------------------------------------------------------------------------


class LiveScheduler:
    def __init__(self, cfg: LiveConfig | None = None) -> None:
        self.cfg = cfg or LiveConfig.from_env()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._delivery_tried: dict[str, str] = {}  # workspace -> local date of the last real-delivery attempt
        self._housekeeping: dict[str, str] = {}  # workspace -> local date housekeeping last ran
        self._analysis_running: str | None = None
        self.started_at: datetime | None = None
        self.last_tick: datetime | None = None
        self.last_error: str = ""

    # lifecycle -------------------------------------------------------------------------------------------
    def start(self) -> None:
        if not self.cfg.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self.started_at = utcnow()
        self._thread = threading.Thread(target=self._loop, name="fieldnote-live", daemon=True)
        self._thread.start()
        log.info(
            "live scheduler started: pulse every %s min, analysis every %s h, delivery after %02d:00",
            self.cfg.pulse_minutes,
            self.cfg.analysis_hours,
            self.cfg.deliver_hour,
        )

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def run_forever(self) -> None:
        """Blocking variant for ``fieldnote live``."""
        self.started_at = utcnow()
        self._loop()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
                self.last_error = ""
            except Exception as exc:  # never let one bad tick stop live mode
                self.last_error = f"{type(exc).__name__}: {exc}"[:300]
                log.exception("live scheduler tick failed")
            self._stop.wait(TICK_SECONDS)

    # one pass ----------------------------------------------------------------------------------------------
    def tick(self) -> list[tuple[str, str]]:
        """Start whatever is due, most overdue first, within the concurrency limit. Returns what started."""
        from fieldnote.dashboard import runner
        from fieldnote.web import context as wc
        from fieldnote.workspaces import list_entries, load

        now = utcnow()
        self.last_tick = now
        started: list[tuple[str, str]] = []
        busy = set(runner.running_procs())
        if self._analysis_running and self._analysis_running not in busy:
            self._analysis_running = None
        candidates: list[tuple[datetime, str, str, bool, bool]] = []
        for entry in list_entries(wc.registry()):
            if not entry.valid:
                continue
            ws = entry.name
            db = wc.db_for(ws)
            if ws in busy or runner.active_run(db, ws) is not None:
                continue
            try:
                cfg_ws = load(ws, wc.registry())
            except ConfigError:
                continue
            sched = schedule(db, ws, self.cfg, now)
            today = str(_local_today(cfg_ws, now))
            deliver = self._delivery_tried.get(ws) != today and delivery_due(db, cfg_ws, self.cfg, now)
            pulse_due = sched["next_pulse"] <= now
            if deliver or sched["next_analysis"] <= now:
                # A workspace that has never been analysed (or owes today's brief) goes first.
                first = deliver or not sched["has_analysis"]
                due = datetime.min if first else sched["next_analysis"]
                candidates.append((due, ws, "analysis", deliver, pulse_due))
            elif pulse_due:
                candidates.append((sched["next_pulse"], ws, "pulse", False, False))
            self._maybe_housekeeping(ws, cfg_ws, now)
        candidates.sort()
        slots = self.cfg.max_concurrent - len(busy)
        for _, ws, kind, deliver, pulse_due in candidates:
            if slots <= 0:
                break
            if kind == "analysis" and self._analysis_running:
                # One analysis at a time keeps free LLM tiers inside their rate limits; refresh the feed instead.
                if not pulse_due:
                    continue
                kind, deliver = "pulse", False
            try:
                runner.launch_run(wc.db_for(ws), ws, kind=kind, deliver=deliver)
            except (runner.RunBusyError, ConfigError, OSError) as exc:
                log.warning("could not start %s for %s: %s", kind, ws, exc)
                continue
            if kind == "analysis":
                self._analysis_running = ws
            if deliver:
                cfg_ws = load(ws, wc.registry())
                self._delivery_tried[ws] = str(_local_today(cfg_ws, now))
            started.append((ws, kind))
            slots -= 1
            log.info("live: started %s for %s%s", kind, ws, " (with delivery)" if deliver else "")
        return started

    def _maybe_housekeeping(self, ws: str, cfg_ws: Any, now: datetime) -> None:
        """Once a local day after the delivery hour: reminders, retention, and the weekly memo on Mondays."""
        today = _local_today(cfg_ws, now)
        if self._housekeeping.get(ws) == str(today) or _local_hour(cfg_ws, now) < self.cfg.deliver_hour:
            return
        self._housekeeping[ws] = str(today)
        jobs = [["remind", "-w", ws], ["purge", "-w", ws]]
        if today.weekday() == 0:
            jobs.append(["brief", "weekly", "-w", ws, "--deliver"])
        threading.Thread(target=_run_jobs, args=(ws, jobs), name=f"fieldnote-housekeeping-{ws}", daemon=True).start()


def _run_jobs(ws: str, jobs: list[list[str]]) -> None:
    for args in jobs:
        try:
            res = subprocess.run(
                [sys.executable, "-m", "fieldnote.cli", *args],
                capture_output=True,
                text=True,
                timeout=15 * 60,
                check=False,
            )
            log.info("live housekeeping %s for %s: exit %s", args[0], ws, res.returncode)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("live housekeeping %s for %s failed: %s", args[0], ws, exc)


_SCHEDULER: LiveScheduler | None = None


def get_scheduler() -> LiveScheduler | None:
    return _SCHEDULER


def start_embedded() -> LiveScheduler | None:
    """Start the scheduler inside this process (the web server), unless FIELDNOTE_SCHEDULER=off."""
    global _SCHEDULER
    cfg = LiveConfig.from_env()
    if not cfg.enabled:
        log.info("live scheduler disabled (FIELDNOTE_SCHEDULER=off); expecting `fieldnote live` elsewhere")
        return None
    if _SCHEDULER is None:
        _SCHEDULER = LiveScheduler(cfg)
    _SCHEDULER.start()
    return _SCHEDULER


def run_worker() -> None:
    """``fieldnote live``: the scheduler on its own, for a separate worker process or container."""
    global _SCHEDULER
    cfg = LiveConfig.from_env()
    _SCHEDULER = LiveScheduler(
        LiveConfig(True, cfg.pulse_minutes, cfg.analysis_hours, cfg.deliver_hour, cfg.max_concurrent)
    )
    try:
        _SCHEDULER.run_forever()
    except KeyboardInterrupt:
        _SCHEDULER.stop()
        time.sleep(0.1)
