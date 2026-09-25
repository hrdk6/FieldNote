"""Full pipeline: collect -> process -> agents -> score -> briefs -> deliver.

Each stage is isolated: failures are logged, recorded in run stats, and the run continues. Re-running
the same day updates the existing run instead of duplicating it. Offline mode replays the bundled
fixture rounds (a baseline round first, if it has not run yet) with the MockClient.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from fieldnote.agents.context import AgentContext
from fieldnote.agents.orchestrator import AgentOrchestrator
from fieldnote.agents.trace import Tracer
from fieldnote.collectors.base import Collector, PoliteHttpClient
from fieldnote.collectors.fixtures import FixtureDataset, fixture_collectors
from fieldnote.collectors.pages import PagesCollector
from fieldnote.collectors.reddit import RedditCollector
from fieldnote.collectors.rss_news import RssNewsCollector
from fieldnote.collectors.youtube import YoutubeCollector
from fieldnote.config import Settings, WorkspaceConfig, apply_fixture_overlay, get_settings, resolve_database_url
from fieldnote.costs import CostTracker
from fieldnote.db import repo
from fieldnote.db.engine import Database, get_database, utcnow
from fieldnote.domain import CollectedDocument, StageRecord
from fieldnote.llm.base import LLMClient, make_llm
from fieldnote.llm.cache import LLMCacheStore
from fieldnote.logging_setup import get_logger, run_id_var, workspace_var
from fieldnote.processing import change_detection, metrics, tagging, themes
from fieldnote.processing.claim_vs_reported import compute_claim_vs_reported
from fieldnote.processing.dedupe import dedupe_documents, titles_match

log = get_logger(__name__)

DEFAULT_LOOKBACK_DAYS = 7
ALL_STAGES = ("collect", "process", "agents", "briefs", "deliver")
# A pulse is a light live refresh: collect and process only, no agents or briefs. A workspace has one pulse
# run per day (runs are unique per workspace, day, mode and kind); each pulse continues it without clearing
# what earlier pulses found, and processes only the items it collected itself.
PULSE_STAGES = ("collect", "process")


@dataclass
class RunSummary:
    workspace: str
    run_id: int
    mode: str
    status: str
    as_of: datetime
    stages: list[StageRecord] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    briefs: dict[str, str] = field(default_factory=dict)
    deliveries: list[dict[str, Any]] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return {"success": 0, "partial": 2, "baseline": 0}.get(self.status, 1)


@dataclass
class RunOptions:
    offline: bool = False
    dry_run: bool = False
    stages: tuple[str, ...] = ALL_STAGES
    weekly: bool = False
    as_of: datetime | None = None
    round_label: str | None = None
    kind: str = "daily"
    llm: LLMClient | None = None


def open_db(offline: bool) -> Database:
    return get_database(resolve_database_url(offline=offline))


def build_collectors(
    settings: Settings, cfg: WorkspaceConfig, db: Database, now: datetime, offline: bool, round_label: str | None
) -> tuple[list[Collector], PoliteHttpClient | None]:
    if offline:
        return list(fixture_collectors(settings, cfg.workspace, round_label or "current", now)), None
    http = PoliteHttpClient(
        settings, per_host_delay=cfg.limits.per_host_delay_seconds, timeout=cfg.limits.request_timeout_seconds, db=db
    )
    return [
        RssNewsCollector(settings, http, now),
        PagesCollector(settings, http, now),
        RedditCollector(settings, http, now),
        YoutubeCollector(settings, http, now),
    ], http


def _merge_near_duplicates(
    s: Any, cfg: WorkspaceConfig, docs: list[CollectedDocument], now: datetime
) -> list[CollectedDocument]:
    """Collapse new news items that duplicate stories already stored (syndication across runs)."""
    recent = repo.documents_between(s, cfg.workspace, now - timedelta(days=14), now, ["news"])
    out = []
    for d in docs:
        if d.source_type == "news":
            dup = next((r for r in recent if r.url != d.url and titles_match(r.title, d.title)), None)
            if dup is not None:
                meta = dict(dup.meta or {})
                meta["syndicated_sources"] = sorted(set(meta.get("syndicated_sources", [])) | {d.source_name})
                meta["also_seen_at"] = sorted(set(meta.get("also_seen_at", [])) | {d.url})
                dup.meta = meta
                continue
        out.append(d)
    return out


class Pipeline:
    def __init__(
        self, cfg: WorkspaceConfig, options: RunOptions, settings: Settings | None = None, db: Database | None = None
    ):
        self.settings = settings or get_settings()
        self.options = options
        self.cfg = apply_fixture_overlay(cfg) if options.offline and not cfg.fixture_mode else cfg
        self.db = db or open_db(options.offline)
        self.cost = CostTracker(budget_usd=self.cfg.limits.max_llm_cost_per_run_usd)
        if options.llm is not None:
            self.llm = options.llm
            self.llm.cost = self.cost
        else:
            self.llm = make_llm(
                self.settings, offline=options.offline, cache=LLMCacheStore(self.db, self.cfg.workspace), cost=self.cost
            )

    # ------------------------------------------------------------------------------------------
    def run(self) -> RunSummary:
        opts = self.options
        cfg = self.cfg
        if opts.offline and opts.round_label is None:
            return self._run_fixture_rounds()
        now = opts.as_of or utcnow()
        run_date = cfg.local_date(now).isoformat()
        mode = "offline" if opts.offline else "live"
        pulse = opts.kind == "pulse"
        with self.db.session() as s:
            if pulse:
                # Collect everything since the last successful collection of any kind.
                prev = repo.last_run(
                    s,
                    cfg.workspace,
                    kinds=("daily", "baseline", "pulse"),
                    statuses=("success", "partial", "baseline"),
                    mode=mode,
                    before_as_of=now,
                )
            else:
                # Analyse since the previous analysis run; a same-day rerun must not narrow its own window.
                same_day = repo.find_run(s, cfg.workspace, run_date, mode, opts.kind)
                prev = repo.last_run(
                    s,
                    cfg.workspace,
                    kinds=("daily", "baseline"),
                    mode=mode,
                    before_as_of=now,
                    exclude_id=same_day.id if same_day is not None and not opts.offline else None,
                )
            since = prev.as_of if prev and prev.as_of else now - timedelta(days=DEFAULT_LOOKBACK_DAYS)
            run = repo.start_run(
                s,
                cfg.workspace,
                run_date,
                mode=mode,
                kind=opts.kind,
                as_of=now,
                reset="collect" in opts.stages and not pulse,
            )
            if pulse:
                run.started_at = utcnow()
                run.finished_at = None
            run_id = run.id
            prior_stats = dict(run.stats or {})
        run_id_var.set(str(run_id))
        workspace_var.set(cfg.workspace)
        log.info(
            "run %s started (%s, as of %s, since %s)",
            run_id,
            mode,
            now.isoformat(timespec="minutes"),
            since.isoformat(timespec="minutes"),
        )
        summary = RunSummary(workspace=cfg.workspace, run_id=run_id, mode=mode, status="running", as_of=now)
        stats: dict[str, Any] = {
            **({} if "collect" in opts.stages else prior_stats),
            "since": since.isoformat(timespec="seconds"),
            "fixture": cfg.fixture_mode,
            "llm": self.llm.name,
        }
        if pulse:
            stats["pulses_today"] = int(prior_stats.get("pulses_today", 0)) + 1
        new_doc_ids: list[int] = []
        http: PoliteHttpClient | None = None

        def stage(name: str, fn: Any) -> None:
            if name not in opts.stages:
                summary.stages.append(StageRecord(name=name, status="skipped", message="not requested"))
                return
            rec = StageRecord(name=name)
            t0 = time.perf_counter()
            try:
                result = fn()
                if isinstance(result, tuple):
                    rec.count, rec.status, rec.message = result
                else:
                    rec.count = result
            except Exception as exc:
                log.exception("stage %s failed", name)
                rec.status = "failed"
                rec.message = f"{type(exc).__name__}: {exc}"[:500]
            rec.seconds = time.perf_counter() - t0
            summary.stages.append(rec)
            try:
                self.llm.cache.flush()
            except Exception:
                log.exception("could not persist LLM cache entries")

        collected: list[CollectedDocument] = []

        def do_collect() -> tuple[int, str, str]:
            nonlocal http
            collectors, http = build_collectors(self.settings, cfg, self.db, now, opts.offline, opts.round_label)
            results = []
            for c in collectors:
                res = c.collect(cfg, since)
                results.append(res)
                collected.extend(res.documents)
            stats["collectors"] = {r.name: {"status": r.status, "message": r.message, **r.stats} for r in results}
            if http is not None:
                stats["http"] = dict(http.stats)
            failed = [r.name for r in results if r.status == "failed"]
            skipped = [r.name for r in results if r.status == "skipped"]
            # Store: dedupe and persist documents; snapshot competitor pages and diff them.
            with self.db.session() as s:
                page_docs = [d for d in collected if d.source_type == "page"]
                other = dedupe_documents([d for d in collected if d.source_type != "page"])
                other = _merge_near_duplicates(s, cfg, other, now)
                new_ids = []
                for d in other:
                    row, created = repo.upsert_document(s, cfg.workspace, d, run_id)
                    if created:
                        new_ids.append(row.id)
                stats["documents_new"] = len(new_ids)
                new_doc_ids.extend(new_ids)
                if pulse:
                    stats["documents_new_today"] = int(prior_stats.get("documents_new_today", 0)) + len(new_ids)
                stats["pages"] = change_detection.process_page_documents(s, cfg, run_id, page_docs, now, self.llm)
            status = "degraded" if failed else "ok"
            msg = (
                ", ".join(f"{n} failed" for n in failed)
                + (", " if failed and skipped else "")
                + ", ".join(f"{n} skipped" for n in skipped)
            )
            return len(collected), status, msg

        def do_process() -> tuple[int, str, str]:
            pstats: dict[str, Any] = {}
            warnings = []
            with self.db.session() as s:
                if pulse:  # this pulse's items only; earlier pulses today already processed theirs
                    new_docs = list(repo.get_documents(s, new_doc_ids).values())
                else:
                    new_docs = repo.documents_for_run(s, cfg.workspace, run_id)
                pstats["documents_new"] = len(new_docs)
                pstats["entities"] = tagging.tag_document_entities(new_docs, cfg, self.llm)
            steps = (
                (
                    "metrics",
                    lambda s: {
                        **metrics.load_metrics(s, cfg, http),
                        "datasets": len(metrics.ensure_dataset_documents(s, cfg, run_id, now)),
                    },
                ),
                ("voice", lambda s: tagging.tag_voice(s, cfg, run_id, self.llm, until=now)),
                ("themes", lambda s: themes.compute_themes(s, cfg, run_id, now, self.llm)),
                ("claim_vs_reported", lambda s: compute_claim_vs_reported(s, cfg, run_id, now, self.llm)),
            )
            # Pulses keep the live feed current; themes and claim-vs-reported are recomputed by analysis runs.
            skip = {"themes", "claim_vs_reported"} if pulse else set()
            for name, fn in steps:
                if name in skip:
                    pstats[name] = {"status": "skipped", "reason": "pulse"}
                    continue
                try:
                    with self.db.session() as s:
                        pstats[name] = fn(s)
                    self.llm.cache.flush()
                except Exception as exc:
                    log.exception("processing step %s failed", name)
                    pstats[name] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:300]}
                    warnings.append(name)
            stats["processing"] = pstats
            return (
                pstats["documents_new"],
                ("degraded" if warnings else "ok"),
                ", ".join(f"{w} failed" for w in warnings),
            )

        agent_state: dict[str, Any] = {}

        def do_agents() -> tuple[int, str, str]:
            if opts.kind == "baseline":
                return 0, "skipped", "baseline round: history only"
            ctx = AgentContext.build(
                cfg, self.db, self.llm, run_id, now, since, Tracer(self.db, cfg.workspace, run_id, self.cost)
            )
            st = AgentOrchestrator(ctx).run()
            agent_state["state"] = st
            stats["agents"] = st.stats
            return (
                len(st.finding_ids),
                ("degraded" if st.degraded else "ok"),
                ("LLM budget exhausted" if st.budget_exhausted else ""),
            )

        def do_briefs() -> tuple[int, str, str]:
            if opts.kind == "baseline":
                return 0, "skipped", "baseline round"
            from fieldnote.reports.daily_brief import build_daily_brief, render_daily_brief
            from fieldnote.reports.weekly_memo import build_weekly_memo, render_weekly_memo

            data = build_daily_brief(self.db, cfg, run_id, now, cost_usd=self.cost.total_cost)
            summary.briefs.update(render_daily_brief(self.db, cfg, data))
            if opts.weekly:
                memo = build_weekly_memo(self.db, cfg, now, run_id=run_id)
                summary.briefs.update(render_weekly_memo(self.db, cfg, memo))
            return len(summary.briefs), "ok", ""

        def do_deliver() -> tuple[int, str, str]:
            if opts.kind == "baseline" or not summary.briefs:
                return 0, "skipped", "nothing to deliver"
            from fieldnote.delivery.dispatch import deliver_briefs

            results = deliver_briefs(self.db, cfg, self.settings, run_id, dry_run=opts.dry_run)
            summary.deliveries = results
            failed = [r for r in results if r["status"] == "failed"]
            return len(results), ("degraded" if failed else "ok"), "; ".join(r["message"] for r in results)[:300]

        stage("collect", do_collect)
        stage("process", do_process)
        stage("agents", do_agents)
        stage("briefs", do_briefs)
        stage("deliver", do_deliver)

        failed = [s for s in summary.stages if s.status == "failed"]
        degraded = [s for s in summary.stages if s.status == "degraded"]
        if opts.kind == "baseline":
            status = "baseline" if not failed else "failed"
        elif len(failed) >= 3 or any(s.name == "collect" and s.status == "failed" for s in failed):
            status = "failed"
        elif failed or degraded:
            status = "partial"
        else:
            status = "success"
        stats["stages"] = [s.as_dict() for s in summary.stages]
        stats["cost"] = self.cost.summary()
        with self.db.session() as s:
            run_row = repo.get_run(s, run_id)
            assert run_row is not None
            repo.finish_run(
                s,
                run_row,
                status=status,
                stats=stats,
                cost_usd=self.cost.total_cost,
                tokens_in=self.cost.tokens_in,
                tokens_out=self.cost.tokens_out,
            )
        summary.status = status
        summary.stats = stats
        summary.cost = self.cost.summary()
        log.info("run %s finished: %s (cost $%.4f)", run_id, status, self.cost.total_cost)
        return summary

    # ------------------------------------------------------------------------------------------
    def _run_fixture_rounds(self) -> RunSummary:
        ds = FixtureDataset(self.cfg.workspace)
        rounds = ds.rounds
        last: RunSummary | None = None
        for i, rnd in enumerate(rounds):
            is_last = i == len(rounds) - 1
            with self.db.session() as s:
                existing = repo.last_run(
                    s,
                    self.cfg.workspace,
                    kinds=("baseline",),
                    statuses=("baseline",),
                    mode="offline",
                )
            if not is_last and existing is not None and existing.as_of and existing.as_of >= rnd.as_of:
                continue  # baseline history already present
            opts = RunOptions(
                offline=True,
                dry_run=self.options.dry_run,
                stages=self.options.stages if is_last else ("collect", "process"),
                weekly=self.options.weekly and is_last,
                as_of=rnd.as_of,
                round_label=rnd.label,
                kind="daily" if is_last else "baseline",
                llm=self.llm,
            )
            sub = Pipeline(self.cfg, opts, self.settings, self.db)
            sub.llm = self.llm
            sub.cost = self.cost
            self.llm.cost = self.cost
            last = sub.run()
        assert last is not None
        return last


def run_pipeline(cfg: WorkspaceConfig, **kwargs: Any) -> RunSummary:
    return Pipeline(cfg, RunOptions(**kwargs)).run()
