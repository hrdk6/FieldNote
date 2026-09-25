"""FieldNote command-line interface (Typer)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from fieldnote import __version__
from fieldnote.config import (
    REPO_ROOT,
    ConfigError,
    WorkspaceConfig,
    apply_fixture_overlay,
    fixtures_dir,
    get_settings,
    out_dir,
    resolve_database_url,
    workspaces_dir,
)
from fieldnote.db.engine import Database, get_database, utcnow
from fieldnote.logging_setup import setup_logging

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (ValueError, OSError):
        pass

app = typer.Typer(help="FieldNote: a configurable AI chief-of-staff.", no_args_is_help=True, add_completion=False)
notes_app = typer.Typer(help="Meeting notes -> action items.", no_args_is_help=True)
actions_app = typer.Typer(help="Action tracker.", no_args_is_help=True)
brief_app = typer.Typer(help="Render briefs.", no_args_is_help=True)
workspace_app = typer.Typer(help="Create, list, import and archive workspaces.", no_args_is_help=True)
app.add_typer(workspace_app, name="workspace")
app.add_typer(notes_app, name="notes")
app.add_typer(actions_app, name="actions")
app.add_typer(brief_app, name="brief")
console = Console(soft_wrap=True)

WorkspaceOpt = typer.Option(None, "--workspace", "-w", help="Workspace slug or path to a workspace YAML.")
OfflineOpt = typer.Option(False, "--offline", help="Use bundled fixtures and the MockClient (no network, no keys).")
AllWorkspacesOpt = typer.Option(False, "--all-workspaces", help="Every active workspace (files and database).")


def _init_logging() -> None:
    s = get_settings()
    setup_logging(s.log_level, s.log_format)


def _cfg(workspace: str | None, offline: bool = False) -> WorkspaceConfig:
    from fieldnote.workspaces import load

    try:
        cfg = load(workspace)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if offline:
        try:
            cfg = apply_fixture_overlay(cfg)
        except ConfigError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
    return cfg


def _db(offline: bool) -> Database:
    return get_database(resolve_database_url(offline=offline))


def _has_fixtures(name: str) -> bool:
    return (fixtures_dir() / name / "manifest.json").exists()


def _workspace_names(workspace: str | None, all_workspaces: bool, offline: bool = False) -> list[str]:
    """One workspace, or every active one (skipping, with a note, those without fixtures when offline)."""
    if not all_workspaces:
        return [workspace or get_settings().default_workspace]
    from fieldnote.workspaces import list_names

    names = list_names()
    if offline:
        skipped = [n for n in names if not _has_fixtures(n)]
        if skipped:
            console.print(f"[dim]offline: skipping workspaces without fixtures: {', '.join(skipped)}[/dim]")
        names = [n for n in names if _has_fixtures(n)]
    return names


def _load_each(names: list[str], offline: bool) -> tuple[list[WorkspaceConfig], int]:
    """Load several workspaces; an invalid one is reported and skipped (exit code 1) instead of aborting."""
    from fieldnote.workspaces import load

    cfgs: list[WorkspaceConfig] = []
    failed = 0
    for name in names:
        try:
            cfg = load(name)
            cfgs.append(apply_fixture_overlay(cfg) if offline else cfg)
        except ConfigError as exc:
            console.print(f"[red]{escape(str(exc))}[/red]")
            failed += 1
    return cfgs, failed


def _llm(db: Database, cfg: WorkspaceConfig, offline: bool) -> Any:
    from fieldnote.costs import CostTracker
    from fieldnote.llm.base import make_llm
    from fieldnote.llm.cache import LLMCacheStore

    return make_llm(
        get_settings(),
        offline=offline,
        cache=LLMCacheStore(db, cfg.workspace),
        cost=CostTracker(budget_usd=cfg.limits.max_llm_cost_per_run_usd),
    )


def _print_run_summary(summary: Any) -> None:
    t = Table(title=f"Run {summary.run_id} · {summary.workspace} · {summary.mode} · {summary.status.upper()}")
    for col in ("stage", "status", "count", "seconds", "message"):
        t.add_column(col)
    for st in summary.stages:
        color = {"ok": "green", "degraded": "yellow", "failed": "red", "skipped": "dim"}.get(st.status, "white")
        t.add_row(
            st.name,
            f"[{color}]{st.status}[/{color}]",
            "" if st.count is None else str(st.count),
            f"{st.seconds:.2f}",
            escape((st.message or "")[:110]),
        )
    console.print(t)
    agents = summary.stats.get("agents", {})
    if agents:
        console.print(
            f"claims: {agents.get('claims', 0)} · verified: {agents.get('verified_claims', 0)} · findings: {agents.get('findings', 0)}"
            f" · LLM: {summary.stats.get('llm')} · cost ${summary.cost.get('cost_usd', 0):.4f}"
        )
    for k, v in summary.briefs.items():
        console.print(f"  {k}: {v}")


# ---------------------------------------------------------------------------------------------
# init / doctor
# ---------------------------------------------------------------------------------------------


@app.command()
def init(
    workspace: str = typer.Option(None, "--workspace", "-w", help="Create workspaces/<slug>.yaml from the template."),
    force: bool = typer.Option(False, help="Overwrite an existing workspace file."),
) -> None:
    """Create .env, data/ and out/ directories, the database schema, and optionally a new workspace."""
    _init_logging()
    env = Path.cwd() / ".env"
    example = REPO_ROOT / ".env.example"
    if not env.exists() and example.exists():
        shutil.copy(example, env)
        console.print(f"created {env} from .env.example (fill in the keys you have; everything is optional)")
    for d in ("data", "out"):
        (Path.cwd() / d).mkdir(exist_ok=True)
    db = _db(False)
    console.print(f"database ready: {db.url.split('@')[-1]}")
    if workspace:
        from fieldnote.config import SLUG_RE

        if not SLUG_RE.match(workspace):
            console.print("[red]workspace slug must be lowercase letters, digits and underscores[/red]")
            raise typer.Exit(1)
        target = workspaces_dir() / f"{workspace}.yaml"
        if target.exists() and not force:
            console.print(f"[yellow]{target} exists; use --force to overwrite[/yellow]")
            raise typer.Exit(1)
        text = (
            (workspaces_dir() / "_template.yaml")
            .read_text(encoding="utf-8")
            .replace("workspace: my_workspace", f"workspace: {workspace}")
        )
        target.write_text(text, encoding="utf-8")
        console.print(f"created {target}; edit it, then run `fieldnote doctor -w {workspace}`")


@app.command()
def doctor(
    workspace: str = WorkspaceOpt,
    no_network: bool = typer.Option(False, "--no-network", help="Skip source reachability checks."),
    check_llm: bool = typer.Option(
        False, "--check-llm", help="Call the configured LLM provider(s) to verify keys and model names."
    ),
) -> None:
    """Validate configs, env vars, credentials, source reachability and robots.txt status."""
    _init_logging()
    from fieldnote.doctor import run_doctor

    rows = run_doctor(workspace, no_network=no_network, check_llm=check_llm)
    t = Table(title="fieldnote doctor")
    for col in ("check", "status", "detail"):
        t.add_column(col)
    worst = "pass"
    for r in rows:
        color = {"pass": "green", "warn": "yellow", "fail": "red"}[r.status]
        t.add_row(escape(r.check), f"[{color}]{r.status.upper()}[/{color}]", escape(r.detail))
        if r.status == "fail":
            worst = "fail"
        elif r.status == "warn" and worst == "pass":
            worst = "warn"
    console.print(t)
    counts = {k: sum(1 for r in rows if r.status == k) for k in ("pass", "warn", "fail")}
    console.print(f"{counts['pass']} pass · {counts['warn']} warn · {counts['fail']} fail")
    raise typer.Exit(1 if worst == "fail" else 0)


# ---------------------------------------------------------------------------------------------
# collect / process / run
# ---------------------------------------------------------------------------------------------


def _pipeline(
    workspace: str | None,
    offline: bool,
    dry_run: bool,
    stages: tuple[str, ...],
    weekly: bool = False,
    kind: str = "daily",
) -> int:
    from fieldnote.pipeline import RunOptions, run_pipeline

    _init_logging()
    cfg = _cfg(workspace)
    opts = RunOptions(offline=offline, dry_run=dry_run, stages=stages, weekly=weekly, kind=kind)
    summary = run_pipeline(cfg, **opts.__dict__)
    _print_run_summary(summary)
    return summary.exit_code


@app.command()
def collect(workspace: str = WorkspaceOpt, offline: bool = OfflineOpt) -> None:
    """Collect and store documents (news, pages, Reddit, YouTube) and snapshot competitor pages."""
    raise typer.Exit(_pipeline(workspace, offline, True, ("collect",)))


@app.command()
def process(workspace: str = WorkspaceOpt, offline: bool = OfflineOpt) -> None:
    """Process today's collected documents: entities, metrics, customer voice, themes, claim vs reported."""
    raise typer.Exit(_pipeline(workspace, offline, True, ("process",)))


@app.command()
def pulse(workspace: str = WorkspaceOpt) -> None:
    """Live refresh: collect and process new items from real sources (no agents, no brief)."""
    from fieldnote.pipeline import PULSE_STAGES

    raise typer.Exit(_pipeline(workspace, False, True, PULSE_STAGES, kind="pulse"))


@app.command()
def live() -> None:
    """Keep every workspace current: pulses, periodic analysis, daily delivery and housekeeping."""
    from fieldnote.live.scheduler import LiveConfig, run_worker

    _init_logging()
    cfg = LiveConfig.from_env()
    console.print(
        f"live mode: pulse every {cfg.pulse_minutes} min, analysis every {cfg.analysis_hours} h, "
        f"delivery after {cfg.deliver_hour:02d}:00 local (Ctrl+C to stop)"
    )
    run_worker()


@app.command()
def run(
    workspace: str = WorkspaceOpt,
    dry_run: bool = typer.Option(False, "--dry-run", help="Write deliveries to out/<ws>/outbox instead of sending."),
    offline: bool = OfflineOpt,
    weekly: bool = typer.Option(False, "--weekly", help="Also render the weekly PDF memo."),
    all_workspaces: bool = AllWorkspacesOpt,
) -> None:
    """Full pipeline: collect -> process -> agents -> score -> briefs -> deliver."""
    stages = ("collect", "process", "agents", "briefs", "deliver")
    if all_workspaces:
        codes = []
        for name in _workspace_names(None, True, offline):
            try:
                codes.append(_pipeline(name, offline, dry_run, stages, weekly))
            except typer.Exit as exc:  # invalid config: already reported; keep going with the others
                codes.append(int(exc.exit_code or 1))
        raise typer.Exit(max(codes) if codes else 0)
    raise typer.Exit(_pipeline(workspace, offline, dry_run, stages, weekly))


# ---------------------------------------------------------------------------------------------
# ask
# ---------------------------------------------------------------------------------------------


@app.command()
def ask(
    question: str = typer.Argument(..., help="Your question, in quotes."),
    workspace: str = WorkspaceOpt,
    offline: bool = OfflineOpt,
    session: str = typer.Option(
        None, "--session", help="Session id for follow-up questions (kept in out/<ws>/ask_sessions)."
    ),
) -> None:
    """Ask FieldNote a question; answers use only the local database and cite sources."""
    from fieldnote.ask.answer import AskSession
    from fieldnote.ask.answer import ask as ask_fn

    _init_logging()
    cfg = _cfg(workspace, offline)
    db = _db(offline)
    llm = _llm(db, cfg, offline)
    sess = AskSession.load(cfg.workspace, session) if session else None
    ans = ask_fn(db, cfg, question, llm=llm, session=sess)
    if sess:
        sess.save(cfg.workspace)
    console.print(ans.markdown, markup=False, highlight=False)
    console.print(f"\nintent: {ans.intent} ({ans.route_via}) · saved to {ans.path}", markup=False, style="dim")


# ---------------------------------------------------------------------------------------------
# notes / actions / remind
# ---------------------------------------------------------------------------------------------


@notes_app.command("add")
def notes_add(
    file: str = typer.Argument("-", help="Markdown/text file with meeting notes, or '-' for stdin."),
    workspace: str = WorkspaceOpt,
    note_date: str = typer.Option(
        None, "--date", help="Meeting date (YYYY-MM-DD); defaults to today in the workspace timezone."
    ),
    title: str = typer.Option("", "--title"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Store without asking for confirmation."),
    no_merge: bool = typer.Option(False, "--no-merge", help="Create new items even when similar open items exist."),
    offline: bool = OfflineOpt,
) -> None:
    """Extract action items from meeting notes, preview them, and store them on confirmation."""
    from fieldnote.coordination.extractor import commit_preview, preview_note

    _init_logging()
    cfg = _cfg(workspace, offline)
    db = _db(offline)
    body = sys.stdin.read() if file == "-" else Path(file).expanduser().read_text(encoding="utf-8")
    if not body.strip():
        console.print("[red]empty note[/red]")
        raise typer.Exit(1)
    nd = date.fromisoformat(note_date) if note_date else cfg.local_date(utcnow())
    prev = preview_note(db, cfg, _llm(db, cfg, offline), body, nd, title or (Path(file).stem if file != "-" else ""))
    t = Table(title=f"Extracted action items ({prev.via}) · note date {nd.isoformat()}")
    for col in ("#", "description", "owner", "due", "priority", "conf", "flags"):
        t.add_column(col)
    for i, it in enumerate(prev.items):
        t.add_row(
            str(i),
            it.description,
            it.owner,
            it.due_date.isoformat() if it.due_date else "-",
            it.priority,
            f"{it.confidence:.2f}",
            ", ".join(it.flags),
        )
    console.print(t)
    for m in prev.merges:
        console.print(
            f"[yellow]item {m.index} looks like open item #{m.existing_id} ({m.score:.0f}%): {m.existing_description}[/yellow]"
        )
    if not prev.items:
        console.print("no action items found")
        raise typer.Exit(0)
    if not yes and not typer.confirm("Store these items?", default=True):
        raise typer.Exit(0)
    res = commit_preview(db, cfg, prev, accept_merges=not no_merge)
    console.print(
        f"stored note #{res['note_id']}: {len(res['created'])} new items, {len(res['merged'])} merged into existing"
    )


@actions_app.command("list")
def actions_list(
    workspace: str = WorkspaceOpt,
    status: str = typer.Option("open", help="open | done | dropped | all"),
    offline: bool = OfflineOpt,
) -> None:
    """List action items."""
    from fieldnote.coordination.tracker import list_items

    cfg = _cfg(workspace, offline)
    rows = list_items(_db(offline), cfg, None if status == "all" else (status,))
    t = Table(title=f"Action items ({status}) · {cfg.display_name}")
    for col in ("id", "description", "owner", "due", "priority", "status", "flags"):
        t.add_column(col)
    for r in rows:
        t.add_row(
            str(r["id"]), r["description"], r["owner"], r["due_date"] or "-", r["priority"], r["status"], r["flags"]
        )
    console.print(t if rows else "no action items")


def _set_status(item_id: int, status: str, workspace: str | None, offline: bool) -> None:
    from fieldnote.coordination.tracker import TrackerError, update_item

    cfg = _cfg(workspace, offline)
    try:
        r = update_item(_db(offline), cfg, item_id, status=status)
    except TrackerError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"#{r['id']} -> {r['status']}: {r['description']}")


@actions_app.command("done")
def actions_done(item_id: int, workspace: str = WorkspaceOpt, offline: bool = OfflineOpt) -> None:
    """Mark an action item done."""
    _set_status(item_id, "done", workspace, offline)


@actions_app.command("drop")
def actions_drop(item_id: int, workspace: str = WorkspaceOpt, offline: bool = OfflineOpt) -> None:
    """Drop an action item."""
    _set_status(item_id, "dropped", workspace, offline)


@actions_app.command("update")
def actions_update(
    item_id: int,
    workspace: str = WorkspaceOpt,
    owner: str = typer.Option(None),
    due: str = typer.Option(None, help="YYYY-MM-DD, or 'none' to clear"),
    priority: str = typer.Option(None),
    offline: bool = OfflineOpt,
) -> None:
    """Edit owner, due date or priority."""
    from fieldnote.coordination.tracker import TrackerError, update_item

    cfg = _cfg(workspace, offline)
    try:
        r = update_item(
            _db(offline),
            cfg,
            item_id,
            owner=owner,
            priority=priority,
            due_date=date.fromisoformat(due) if due and due != "none" else None,
            clear_due=due == "none",
        )
    except (TrackerError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(json.dumps(r, indent=1))


@actions_app.command("export")
def actions_export(
    workspace: str = WorkspaceOpt,
    csv_path: str = typer.Option(None, "--csv", help="Output CSV path (default out/<ws>/action_items.csv)."),
    sheets: bool = typer.Option(False, "--sheets", help="Also sync to Google Sheets if configured."),
    offline: bool = OfflineOpt,
) -> None:
    """Export the tracker to CSV (and optionally Google Sheets)."""
    from fieldnote.coordination.sheets_export import SheetsUnavailableError, sync_to_sheets
    from fieldnote.coordination.tracker import export_csv

    cfg = _cfg(workspace, offline)
    db = _db(offline)
    path = export_csv(db, cfg, Path(csv_path) if csv_path else None)
    console.print(f"wrote {path}")
    if sheets:
        try:
            n = sync_to_sheets(db, cfg, get_settings())
            console.print(f"synced {n} rows to Google Sheets")
        except SheetsUnavailableError as exc:
            console.print(f"[yellow]{exc}[/yellow]")


@app.command()
def remind(
    workspace: str = WorkspaceOpt,
    dry_run: bool = typer.Option(False, "--dry-run"),
    within_days: int = typer.Option(None, "--within-days", help="Override reminders.due_within_days."),
    offline: bool = OfflineOpt,
    all_workspaces: bool = AllWorkspacesOpt,
) -> None:
    """Send a digest of overdue and soon-due action items (at most once per item per day)."""
    from fieldnote.coordination.reminders import run_reminders

    _init_logging()
    if all_workspaces:
        cfgs, failed = _load_each(_workspace_names(None, True, offline), offline)
    else:
        cfgs, failed = [_cfg(workspace, offline)], 0
    for cfg in cfgs:
        if all_workspaces:
            console.rule(cfg.workspace)
        res = run_reminders(_db(offline), cfg, get_settings(), dry_run=dry_run, within_days=within_days)
        if not res.overdue and not res.due_soon:
            console.print(f"nothing to remind ({res.skipped_already_reminded} already reminded today)")
            continue
        console.print(res.text, markup=False)
        for d in res.deliveries:
            console.print(f"[dim]{d['channel']}: {d['status']} - {d['message']}[/dim]")
    if failed:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------------------------
# briefs / eval / dashboard / demo / export / purge
# ---------------------------------------------------------------------------------------------


@brief_app.command("daily")
def brief_daily(
    workspace: str = WorkspaceOpt,
    deliver: bool = typer.Option(False, "--deliver", help="Deliver after rendering (dry-run unless configured)."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    offline: bool = OfflineOpt,
) -> None:
    """Re-render the daily brief for the latest run."""
    from fieldnote.db import repo
    from fieldnote.delivery.dispatch import deliver_briefs
    from fieldnote.reports.daily_brief import build_daily_brief, render_daily_brief

    _init_logging()
    cfg = _cfg(workspace, offline)
    db = _db(offline)
    with db.session() as s:
        run = repo.last_run(s, cfg.workspace, kinds=("daily",))
        if run is None:
            console.print("[red]no completed run yet; run `fieldnote run` first[/red]")
            raise typer.Exit(1)
        run_id, as_of = run.id, run.as_of or utcnow()
    paths = render_daily_brief(db, cfg, build_daily_brief(db, cfg, run_id, as_of))
    for k, v in paths.items():
        console.print(f"{k}: {v}")
    if deliver:
        for r in deliver_briefs(db, cfg, get_settings(), run_id, dry_run=dry_run, kinds=("daily",)):
            console.print(f"{r['channel']}: {r['status']} - {r['message']}")


@brief_app.command("weekly")
def brief_weekly(
    workspace: str = WorkspaceOpt,
    deliver: bool = typer.Option(False, "--deliver", help="Deliver after rendering (dry-run unless configured)."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    offline: bool = OfflineOpt,
    all_workspaces: bool = AllWorkspacesOpt,
) -> None:
    """Render the weekly decision memo (PDF) for the last 7 days."""
    from fieldnote.db import repo
    from fieldnote.delivery.dispatch import deliver_briefs
    from fieldnote.reports.weekly_memo import build_weekly_memo, render_weekly_memo

    _init_logging()
    if all_workspaces:
        cfgs, failed = _load_each(_workspace_names(None, True, offline), offline)
    else:
        cfgs, failed = [_cfg(workspace, offline)], 0
    db = _db(offline)
    for cfg in cfgs:
        with db.session() as s:
            run = repo.last_run(s, cfg.workspace, kinds=("daily",))
            as_of = run.as_of if run and run.as_of else utcnow()
            run_id = run.id if run else None
        if all_workspaces and run is None:
            console.print(f"[dim]{cfg.workspace}: no completed run yet; memo skipped[/dim]")
            continue
        paths = render_weekly_memo(db, cfg, build_weekly_memo(db, cfg, as_of, run_id=run_id))
        for k, v in paths.items():
            console.print(f"{k}: {v}")
        if deliver:
            for r in deliver_briefs(db, cfg, get_settings(), None, dry_run=dry_run, kinds=("weekly",)):
                console.print(f"{r['channel']}: {r['status']} - {r['message']}")
    if failed:
        raise typer.Exit(1)


@app.command("eval")
def eval_cmd(
    workspace: str = WorkspaceOpt,
    all_workspaces: bool = typer.Option(False, "--all", help="Evaluate every workspace."),
    offline: bool = typer.Option(
        True, "--offline/--live-db", help="Evaluate the demo/offline database (default) or the live one."
    ),
    live_llm: bool = typer.Option(
        False, "--live-llm", help="Also measure the critic's catch rate with the real LLM (needs a key)."
    ),
    update_readme: bool = typer.Option(False, "--update-readme", help="Write the results table into README.md."),
) -> None:
    """Run evals and write out/eval_report.md."""
    from fieldnote.evals.run_evals import run_evals, write_eval_report
    from fieldnote.evals.run_evals import update_readme as upd

    _init_logging()
    names = _workspace_names(workspace, all_workspaces, offline)
    db = _db(offline)
    reports = []
    for name in names:
        cfg = _cfg(name, offline)
        reports.append(
            run_evals(
                db,
                cfg,
                get_settings(),
                llm=_llm(db, cfg, True) if offline else None,
                live_llm=live_llm,
                write_report=False,
            )
        )
    path = write_eval_report(reports)
    from fieldnote.evals.run_evals import markdown_table

    console.print(markdown_table(reports))
    console.print(f"report: {path}")
    if update_readme and upd(reports):
        console.print("README.md eval table updated")
    raise typer.Exit(0 if all(r.passed for r in reports) else 1)


@app.command()
def dashboard(
    port: int = typer.Option(8501, help="Port for the dashboard."),
    host: str = typer.Option("127.0.0.1", help="Interface to bind (0.0.0.0 inside a container)."),
    demo: bool = typer.Option(False, "--demo", help="Show the bundled fixture dataset instead of live data."),
    scheduler: bool = typer.Option(
        True, "--scheduler/--no-scheduler", help="Run live collection inside this process (FIELDNOTE_SCHEDULER)."
    ),
    legacy: bool = typer.Option(False, "--legacy", help="Launch the old Streamlit dashboard instead."),
) -> None:
    """Launch the dashboard: the web app, its JSON API and (by default) the live scheduler."""
    env = dict(os.environ)
    if demo and not env.get("DATABASE_URL"):
        env["DATABASE_URL"] = resolve_database_url(offline=True)
    if not legacy:
        from fieldnote.web.server import STATIC_DIR, serve

        _init_logging()
        if not (STATIC_DIR / "index.html").exists():
            console.print("[yellow]the web UI is not built; run `npm install && npm run build` in web/[/yellow]")
        if demo:
            os.environ["FIELDNOTE_DASHBOARD_DEMO"] = "1"
            if not os.environ.get("DATABASE_URL"):
                os.environ["DATABASE_URL"] = env["DATABASE_URL"]
        if demo or not scheduler:
            os.environ["FIELDNOTE_SCHEDULER"] = "off"
        mode = "demo data" if demo else "live data"
        console.print(f"starting dashboard on http://localhost:{port} with {mode} (Ctrl+C to stop)")
        serve(host=host, port=port)
        return
    app_path = Path(__file__).resolve().parent / "dashboard" / "app.py"
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app_path),
        "--server.port",
        str(port),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
        "--theme.primaryColor",
        "#2a78d6",
    ]
    console.print(f"starting dashboard on http://localhost:{port} (Ctrl+C to stop)")
    raise typer.Exit(subprocess.call(cmd, env=env))


@app.command()
def demo(
    skip_evals: bool = typer.Option(False, "--skip-evals"),
    update_readme: bool = typer.Option(False, "--update-readme"),
) -> None:
    """Run everything on bundled fixtures with the MockClient (no keys, no network)."""
    from sqlalchemy import select

    from fieldnote.ask.answer import ask as ask_fn
    from fieldnote.coordination.extractor import commit_preview, preview_note
    from fieldnote.coordination.reminders import run_reminders
    from fieldnote.db import repo
    from fieldnote.db.models import MeetingNote
    from fieldnote.evals.extraction_evals import load_labels
    from fieldnote.evals.run_evals import markdown_table, run_evals, write_eval_report
    from fieldnote.evals.run_evals import update_readme as upd
    from fieldnote.pipeline import run_pipeline

    _init_logging()
    os.environ.setdefault("FIELDNOTE_LLM", "mock")
    db = _db(True)
    reports = []
    from fieldnote.workspaces import list_names, load

    available = list_names()
    for name in ("ev_two_wheelers_india", "budget_smartphones_india"):
        if name not in available:
            continue
        console.rule(f"[bold]{name}[/bold]")
        summary = run_pipeline(load(name), offline=True, dry_run=True, weekly=True)
        _print_run_summary(summary)
        cfg = apply_fixture_overlay(load(name))
        llm = _llm(db, cfg, True)
        # Seed the action tracker with a few sample meeting notes (idempotent by title).
        with db.session() as s:
            existing_titles = set(s.scalars(select(MeetingNote.title).where(MeetingNote.workspace == cfg.workspace)))
        for note in load_labels()[:4]:
            title = f"Sample: {note['file']}"
            if title in existing_titles:
                continue
            prev = preview_note(db, cfg, llm, note["body"], date.fromisoformat(note["note_date"]), title)
            commit_preview(db, cfg, prev)
        rem = run_reminders(db, cfg, get_settings(), dry_run=True, now=summary.as_of)
        console.print(f"reminders: {len(rem.overdue)} overdue, {len(rem.due_soon)} due soon (dry run)")
        question = "What changed in competitor pricing this week?"
        ans = ask_fn(db, cfg, question, llm=llm, now=summary.as_of)
        console.print(f"Ask: {question}", style="bold", markup=False)
        console.print(ans.markdown[:1200], markup=False, highlight=False)
        if not skip_evals:
            reports.append(run_evals(db, cfg, get_settings(), llm=llm, write_report=False))
        with db.session() as s:
            console.print(f"documents stored: {repo.count_documents(s, cfg.workspace)}")
    if reports:
        path = write_eval_report(reports)
        console.print(markdown_table(reports))
        console.print(f"eval report: {path}")
        if update_readme and upd(reports):
            console.print("README.md eval table updated")
    console.print(f"\nDemo database: {resolve_database_url(offline=True)}")
    console.print(f"Outputs: {out_dir()}")
    console.print("Next: [bold]fieldnote dashboard --demo[/bold]  (or `make dashboard`)")


@app.command()
def export(
    workspace: str = WorkspaceOpt,
    fmt: str = typer.Option("csv", "--format", help="csv | json"),
    offline: bool = OfflineOpt,
) -> None:
    """Export findings, claims, documents metadata, change events and action items."""
    from fieldnote.exporter import export_workspace

    cfg = _cfg(workspace, offline)
    paths = export_workspace(_db(offline), cfg, fmt)
    for p in paths:
        console.print(f"wrote {p}")


@app.command()
def purge(
    workspace: str = WorkspaceOpt,
    dry_run: bool = typer.Option(False, "--dry-run", help="Only report what would be deleted."),
    offline: bool = OfflineOpt,
    all_workspaces: bool = AllWorkspacesOpt,
) -> None:
    """Retention job: delete posts/documents older than retention_days."""
    _init_logging()
    if all_workspaces:
        cfgs, failed = _load_each(_workspace_names(None, True, offline), offline)
    else:
        cfgs, failed = [_cfg(workspace, offline)], 0
    for cfg in cfgs:
        if all_workspaces:
            console.print(f"[bold]{cfg.workspace}[/bold]")
        _purge_one(cfg, dry_run, offline)
    if failed:
        raise typer.Exit(1)


def _purge_one(cfg: WorkspaceConfig, dry_run: bool, offline: bool) -> None:
    from fieldnote.db import repo

    db = _db(offline)
    now = utcnow()
    if dry_run:
        with db.session() as s:
            posts_cut = now - timedelta(days=cfg.retention_days.posts)
            docs_cut = now - timedelta(days=cfg.retention_days.documents)
            docs = repo.all_documents(s, cfg.workspace)
            n_posts = sum(
                1
                for d in docs
                if d.source_type in ("reddit", "youtube") and (d.published_at or d.fetched_at) < posts_cut
            )
            n_docs = sum(
                1
                for d in docs
                if d.source_type not in ("reddit", "youtube") and (d.published_at or d.fetched_at) < docs_cut
            )
        console.print(f"would delete {n_posts} posts and {n_docs} other documents")
        return
    with db.session() as s:
        res = repo.purge_documents(s, cfg.workspace, now, cfg.retention_days.posts, cfg.retention_days.documents)
    console.print(f"deleted {res['posts']} posts and {res['documents']} other documents")


# ---------------------------------------------------------------------------------------------
# workspace management
# ---------------------------------------------------------------------------------------------


def _print_checks(checks: list[Any]) -> None:
    if not checks:
        return
    t = Table(title="Source verification")
    for col in ("kind", "status", "source", "detail"):
        t.add_column(col)
    for c in checks:
        color = {"ok": "green", "dropped": "red", "unverified": "yellow"}[c.status]
        t.add_row(c.kind, f"[{color}]{c.status}[/{color}]", escape(c.value), escape(c.detail))
    console.print(t)


@workspace_app.command("templates")
def workspace_templates(
    query: str = typer.Argument("", help="Optional search words, e.g. 'payments' or 'Zomato'."),
    category: str = typer.Option(None, "--category", help="Only this category."),
    region: str = typer.Option("India", "--region", help="Show example players for this region."),
) -> None:
    """Browse the workspace library (use one with `fieldnote workspace new --template ID --region R`)."""
    from fieldnote.builder.catalog import CATEGORIES, TEMPLATES, search

    if category and category not in CATEGORIES:
        console.print(f"[red]unknown category; choose from: {escape(', '.join(CATEGORIES))}[/red]")
        raise typer.Exit(1)
    matches = search(query, category)
    t = Table(title=f"Workspace library ({len(matches)} of {len(TEMPLATES)} topics) · example players for {region}")
    t.add_column("id", no_wrap=True, min_width=max([len(m.id) for m in matches] + [2]))
    for col in ("topic", "category", "players"):
        t.add_column(col, overflow="fold")
    for m in matches:
        players = ", ".join(m.players_for(region)) or "(chosen by the model)"
        t.add_row(m.id, escape(m.title), escape(m.category), escape(players))
    console.print(t)


@workspace_app.command("new")
def workspace_new(
    description: str = typer.Argument(None, help="What to track, in plain language (quote it)."),
    template: str = typer.Option(None, "--template", "-t", help="Start from a library topic (see `templates`)."),
    region: str = typer.Option(None, "--region", help="Country or 'Global' (default: inferred)."),
    perspective: str = typer.Option(None, "--perspective", help="Viewpoint for briefs, e.g. 'new entrant'."),
    focal_company: str = typer.Option(None, "--focal-company", help="Company the briefs are written for."),
    name: str = typer.Option(None, "--id", help="Workspace id (slug); default derived from the title."),
    language: str = typer.Option(None, "--language", help="ISO 639-1 language code, e.g. en."),
    no_verify: bool = typer.Option(False, "--no-verify", help="Skip network checks (no URLs are added)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Save without asking for confirmation."),
    print_only: bool = typer.Option(False, "--print", help="Print the YAML and exit without saving."),
) -> None:
    """Draft a workspace for any market, company set, technology or topic, verify its sources, and save it."""
    from fieldnote.builder.core import BuilderError, BuildRequest, build_workspace
    from fieldnote.costs import CostTracker
    from fieldnote.llm.base import make_llm
    from fieldnote.llm.cache import LLMCacheStore
    from fieldnote.workspaces import list_entries, registry_db, save

    _init_logging()
    if template:
        from fieldnote.builder.catalog import get_template

        try:
            tmpl = get_template(template)
        except KeyError as exc:
            console.print(f"[red]{escape(str(exc.args[0]))}[/red]")
            raise typer.Exit(1) from exc
        region = region or "Global"
        description = description or tmpl.description(region)
    if not description:
        console.print("[red]describe what to track, or pass --template (see `fieldnote workspace templates`)[/red]")
        raise typer.Exit(1)
    settings = get_settings()
    db = registry_db()
    llm = make_llm(settings, cache=LLMCacheStore(db, "_builder"), cost=CostTracker(budget_usd=0.5))
    taken = {e.name for e in list_entries(db, include_archived=True)}
    req = BuildRequest(description, region, perspective, focal_company, name, language)
    try:
        result = build_workspace(
            req,
            llm=llm,
            settings=settings,
            taken=taken,
            verify=not no_verify,
            progress=lambda m: console.print(f"[dim]{escape(m)}[/dim]"),
        )
    except BuilderError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from exc
    finally:
        llm.cache.flush()
    cfg = result.config
    console.print(
        f"[bold]{escape(cfg['display_name'])}[/bold]  (id: {cfg['workspace']}, draft: {escape(result.drafted_by)})"
    )
    console.print(f"entities: {escape(', '.join(c['name'] for c in cfg['competitors']))}")
    console.print(f"aspects: {', '.join(cfg['aspects'])}")
    news = cfg["sources"]["news"]
    console.print(
        f"news search: {escape(', '.join(news['search_queries']) or '(none)')} · feeds: {len(news['rss_feeds'])}"
        f" · pages: {sum(len(c['pages']) for c in cfg['competitors'])}"
    )
    _print_checks(result.checks)
    for w in result.warnings:
        console.print(f"[yellow]{escape(w)}[/yellow]")
    if print_only:
        console.print(result.yaml_text, markup=False, highlight=False)
        return
    if not yes and not typer.confirm(f"Save workspace '{result.name}'?", default=True):
        console.print("not saved")
        raise typer.Exit(1)
    try:
        saved = save(result.yaml_text, origin="builder", description=description, db=db)
    except ConfigError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from exc
    where = str(saved.file_path) if saved.file_path else "the database (file not writable)"
    console.print(f"saved to {escape(where)}")
    console.print(f"next: [bold]fieldnote run -w {saved.name} --dry-run[/bold]")


@workspace_app.command("list")
def workspace_list(
    include_archived: bool = typer.Option(False, "--all", help="Include archived workspaces."),
) -> None:
    """List workspaces from workspaces/ and the database."""
    from fieldnote.db import repo
    from fieldnote.workspaces import list_entries, registry_db

    db = registry_db()
    entries = list_entries(db, include_archived=include_archived)
    t = Table(title="Workspaces")
    # ids are what people copy into other commands: never truncate them.
    t.add_column("id", no_wrap=True, min_width=max([len(e.name) for e in entries] + [2]))
    for col in ("name", "stored in", "status", "last run"):
        t.add_column(col, overflow="fold")
    demo_url = resolve_database_url(offline=True)
    demo = get_database(demo_url) if demo_url != db.url and Path(demo_url.removeprefix("sqlite:///")).exists() else None
    for e in entries:
        last = "-"
        for label, source_db in (("", db), (" demo", demo)):
            if source_db is None:
                continue
            with source_db.session() as s:
                run = repo.last_run(s, e.name, kinds=("daily",), statuses=("success", "partial", "failed"))
                if run is not None:
                    last = f"{run.run_date} ({run.status}{label})"
                    break
        status = e.status if e.valid else f"[red]invalid[/red]: {escape(e.error.splitlines()[0][:80])}"
        t.add_row(e.name, escape(e.display_name), e.storage, status, last)
    console.print(t)


@workspace_app.command("show")
def workspace_show(workspace: str = typer.Argument(..., help="Workspace id.")) -> None:
    """Print a workspace's effective YAML (file or database copy)."""
    from fieldnote.workspaces import get_yaml

    try:
        text, source = get_yaml(workspace)
    except ConfigError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[dim]# source: {source}[/dim]")
    console.print(text, markup=False, highlight=False)


@workspace_app.command("import")
def workspace_import(
    file: Path = typer.Argument(..., exists=True, dir_okay=False, help="Workspace YAML file."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing workspace with the same id."),
) -> None:
    """Validate a workspace YAML and store it (database + workspaces/), e.g. for a hosted deployment."""
    from fieldnote.workspaces import save

    text = file.read_text(encoding="utf-8")
    try:
        saved = save(text, origin="import", overwrite=overwrite)
    except ConfigError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"imported '{saved.name}'" + (f" -> {saved.file_path}" if saved.file_path else " (database only)"))


@workspace_app.command("archive")
def workspace_archive(
    workspace: str = typer.Argument(..., help="Workspace id."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
) -> None:
    """Hide a workspace from lists and scheduled runs. Its collected data is kept."""
    from fieldnote.workspaces import archive

    if not yes and not typer.confirm(f"Archive '{workspace}'? (data is kept; restore any time)", default=False):
        raise typer.Exit(1)
    try:
        archive(workspace)
    except ConfigError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"archived '{workspace}'")


@workspace_app.command("restore")
def workspace_restore(workspace: str = typer.Argument(..., help="Workspace id.")) -> None:
    """Bring an archived workspace back."""
    from fieldnote.workspaces import restore

    try:
        restore(workspace)
    except ConfigError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"restored '{workspace}'")


@app.command()
def version() -> None:
    """Print the FieldNote version."""
    console.print(f"FieldNote {__version__}")


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
