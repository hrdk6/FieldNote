"""FieldNote web API (Starlette) and the single-page UI it serves.

Run with ``fieldnote dashboard``. Every page of the UI reads one JSON endpoint under ``/api/ws/<workspace>/``;
all arithmetic and ranking stays in the Python package, exactly as the briefs compute it.
"""

from __future__ import annotations

import contextlib
import dataclasses
import difflib
import hashlib
import hmac
import json
import threading
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response
from starlette.routing import Route

from fieldnote.config import ConfigError, get_settings, out_dir
from fieldnote.db import repo
from fieldnote.web import context as wc
from fieldnote.web import live_api

STATIC_DIR = Path(__file__).resolve().parent / "static"
AUTH_COOKIE = "fn_auth"
MAX_IMPORT_BYTES = 200_000


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# ---- plumbing ---------------------------------------------------------------------------------------


def _default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat(timespec="seconds")
    if isinstance(o, date):
        return o.isoformat()
    if isinstance(o, Path):
        return str(o)
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return dataclasses.asdict(o)
    if isinstance(o, set | tuple):
        return list(o)
    return str(o)


def J(data: Any, status: int = 200) -> Response:
    return Response(json.dumps(data, default=_default), status_code=status, media_type="application/json")


async def body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ApiError("The request body is not valid JSON.") from exc
    if not isinstance(data, dict):
        raise ApiError("The request body must be a JSON object.")
    return data


def ctx_of(request: Request) -> wc.Ctx:
    ws = request.path_params["ws"]
    try:
        return wc.build(ws)
    except ConfigError as exc:
        raise ApiError(str(exc), 404) from exc


def require_edit() -> None:
    if get_settings().dashboard_readonly:
        raise ApiError("This dashboard is read-only (FIELDNOTE_DASHBOARD_READONLY).", 403)


def _token(password: str) -> str:
    return hmac.new(password.encode("utf-8"), b"fieldnote-dashboard", hashlib.sha256).hexdigest()


def _authed(request: Request) -> bool:
    password = get_settings().dashboard_password
    if not password:
        return True
    return hmac.compare_digest(request.cookies.get(AUTH_COOKIE, ""), _token(password))


class AuthMiddleware(BaseHTTPMiddleware):
    OPEN = ("/api/meta", "/api/login")

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        path = request.url.path
        if path.startswith("/api/") and path not in self.OPEN and not _authed(request):
            return J({"error": "Sign in first."}, 401)
        try:
            return await call_next(request)
        except ApiError as exc:
            return J({"error": str(exc)}, exc.status)


def _errors(fn: Any) -> Any:
    """Turn domain errors into 400s with their message (the UI shows it verbatim)."""
    import functools
    import inspect

    from fieldnote.builder.core import BuilderError
    from fieldnote.coordination.tracker import TrackerError

    known = (ApiError, ConfigError, TrackerError, BuilderError)

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def wrapped_async(request: Request) -> Response:
            try:
                return await fn(request)
            except known as exc:
                return J({"error": str(exc)}, getattr(exc, "status", 400))

        return wrapped_async

    @functools.wraps(fn)
    async def wrapped(request: Request) -> Response:
        from starlette.concurrency import run_in_threadpool

        try:
            return await run_in_threadpool(fn, request)
        except known as exc:
            return J({"error": str(exc)}, getattr(exc, "status", 400))

    return wrapped


# ---- shell ------------------------------------------------------------------------------------------


def meta(request: Request) -> Response:
    from fieldnote.builder.catalog import TEMPLATES
    from fieldnote.workspaces import list_entries

    settings = get_settings()
    authed = _authed(request)
    out: dict[str, Any] = {
        "auth_required": bool(settings.dashboard_password),
        "authed": authed,
        "readonly": settings.dashboard_readonly,
        "library_count": len(TEMPLATES),
        "demo": wc.demo_mode(),
    }
    if authed:
        entries = list_entries(wc.registry())
        out["workspaces"] = [
            {
                "name": e.name,
                "display_name": e.display_name,
                "valid": e.valid,
                "error": e.error.splitlines()[0][:200] if e.error else "",
            }
            for e in entries
        ]
        out["default_workspace"] = settings.default_workspace
    return J(out)


async def login(request: Request) -> Response:
    data = await body(request)
    password = get_settings().dashboard_password
    if not password:
        return J({"ok": True})
    entered = str(data.get("password", ""))
    if not hmac.compare_digest(entered.encode("utf-8"), password.encode("utf-8")):
        return J({"error": "Wrong password."}, 401)
    resp = J({"ok": True})
    resp.set_cookie(AUTH_COOKIE, _token(password), httponly=True, samesite="strict", max_age=60 * 60 * 24 * 30)
    return resp


def _run_dict(r: Any) -> dict[str, Any] | None:
    if r is None:
        return None
    return {
        "id": r.id,
        "run_date": r.run_date,
        "mode": r.mode,
        "kind": r.kind,
        "status": r.status,
        "cost_usd": r.cost_usd or 0.0,
        "tokens_in": r.tokens_in or 0,
        "tokens_out": r.tokens_out or 0,
        "started_at": r.started_at,
        "finished_at": r.finished_at,
        "as_of": r.as_of,
        "stats": r.stats or {},
    }


def workspace_context(request: Request) -> Response:
    ctx = ctx_of(request)
    cfg = ctx.cfg
    url = wc.database_url(cfg.workspace)
    return J(
        {
            "workspace": cfg.workspace,
            "display_name": cfg.display_name,
            "fixture": ctx.fixture,
            "now": ctx.now,
            "database": url.split("@")[-1].split("/")[-1],
            "competitors": cfg.competitor_names(),
            "run": {k: v for k, v in (_run_dict(ctx.run) or {}).items() if k != "stats"} if ctx.run else None,
            "weights": dict(cfg.scoring.weights),
            "top_n": cfg.scoring.top_n_in_brief,
            "demo": wc.demo_mode(),
        }
    )


# ---- insights ---------------------------------------------------------------------------------------


def _evidence(items: list[Any]) -> list[dict[str, Any]]:
    return [{**dataclasses.asdict(e), "show_quote": e.show_quote} for e in items]


def _finding(f: Any, s: Any) -> dict[str, Any]:
    from fieldnote.reports.common import evidence_items
    from fieldnote.scoring.opportunities import CRITERIA

    return {
        "id": f.id,
        "title": f.title,
        "category": f.category,
        "status": f.status,
        "scores": {k: float((f.scores or {}).get(k, 0.0)) for k in CRITERIA},
        "evidence_count": f.evidence_count,
        "first_seen": f.first_seen,
        "last_seen": f.last_seen,
        "raw": f.raw_ratings or {},
        "disputed": bool((f.checks or {}).get("disputed")),
        "counter_evidence": [j.get("note", "") for j in ((f.checks or {}).get("counter_evidence") or [])],
        "what": f.what_happened,
        "why": f.why_it_matters,
        "action": f.recommended_action,
        "how": f.how_to_execute,
        "entities": f.entities or [],
        "evidence": _evidence(evidence_items(s, list(f.evidence_claim_ids or []))),
    }


def overview(request: Request) -> Response:
    from fieldnote.reports.common import verification_stats
    from fieldnote.scoring.opportunities import rank

    ctx = ctx_of(request)
    cfg = ctx.cfg
    with ctx.db.session() as s:
        total_docs = repo.count_documents(s, cfg.workspace)
        recent_docs = len(repo.documents_between(s, cfg.workspace, ctx.now - timedelta(days=7), ctx.now))
        changes = repo.changes_between(s, cfg.workspace, ctx.now - timedelta(days=7), ctx.now)
        by_comp: dict[str, int] = defaultdict(int)
        for c in changes:
            by_comp[c.competitor] += 1
        ver = verification_stats(s, cfg.workspace, [ctx.run_id]) if ctx.run_id else {"verified_pct": 0.0, "checked": 0}
        open_actions = repo.open_action_items(s, cfg.workspace)
        overdue = sum(1 for a in open_actions if a.due_date and a.due_date < cfg.local_date(ctx.now))
        rows = repo.findings(s, cfg.workspace, statuses=("active", "demoted"), run_id=ctx.run_id) if ctx.run_id else []
        ranked = rank(
            [
                {
                    "id": f.id,
                    "title": f.title,
                    "scores": f.scores or {},
                    "evidence_count": f.evidence_count,
                    "last_seen": f.last_seen,
                    "row": f,
                }
                for f in rows
            ],
            cfg.scoring.weights,
        )[: cfg.scoring.top_n_in_brief]
        top = [{**_finding(x["row"], s), "rank": x["rank"], "total": x["total"]} for x in ranked]
        n_findings = len(rows)
    return J(
        {
            "kpis": {
                "documents": total_docs,
                "documents_7d": recent_docs,
                "changes_7d": len(changes),
                "changes_by_competitor": dict(by_comp),
                "verified_pct": ver.get("verified_pct", 0.0),
                "checked": ver.get("checked", 0),
                "open_actions": len(open_actions),
                "overdue_actions": overdue,
            },
            "findings_total": n_findings,
            "top": top,
        }
    )


def _diff(before: str, after: str) -> tuple[list[list[str]], list[list[str]]]:
    sm = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    b_out: list[list[str]] = []
    a_out: list[list[str]] = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            b_out.append(["eq", before[i1:i2]])
            a_out.append(["eq", after[j1:j2]])
        else:
            if i2 > i1:
                b_out.append(["del", before[i1:i2]])
            if j2 > j1:
                a_out.append(["ins", after[j1:j2]])
    return b_out, a_out


def changes(request: Request) -> Response:
    ctx = ctx_of(request)
    days = max(1, min(int(request.query_params.get("days", "30")), 3650))
    with ctx.db.session() as s:
        rows = repo.changes_between(s, ctx.cfg.workspace, ctx.now - timedelta(days=days), ctx.now)
        out = []
        for r in rows:
            b, a = _diff(r.before or "", r.after or "")
            out.append(
                {
                    "id": r.id,
                    "competitor": r.competitor,
                    "kind": r.kind,
                    "significance": r.significance,
                    "summary": r.summary,
                    "url": r.url,
                    "detected_at": r.detected_at,
                    "before": b,
                    "after": a,
                    "run_id": r.run_id,
                }
            )
    return J({"days": days, "changes": out, "pages_watched": sum(len(c.pages) for c in ctx.cfg.competitors)})


def metrics(request: Request) -> Response:
    from fieldnote.processing.metrics import metric_summaries

    ctx = ctx_of(request)
    cfg = ctx.cfg
    if not cfg.connectors:
        return J({"configured": False, "summaries": [], "points": []})
    with ctx.db.session() as s:
        summaries = metric_summaries(s, cfg)
        points = [
            {"connector": p.connector, "entity": p.entity, "region": p.region, "period": p.period, "value": p.value}
            for p in repo.metric_points(s, cfg.workspace)
        ]
    return J(
        {
            "configured": True,
            "summaries": [
                {
                    "name": x.name,
                    "label": x.label,
                    "unit": x.unit,
                    "unit_short": x.unit_short,
                    "source_url": x.source_url,
                    "periods": x.periods,
                    "latest_period": x.latest_period,
                    "previous_period": x.previous_period,
                    "total_latest": x.total_latest,
                    "total_previous": x.total_previous,
                    "movers": x.movers,
                    "entities": [dataclasses.asdict(e) for e in x.entities],
                }
                for x in summaries
            ],
            "points": points,
        }
    )


def voice(request: Request) -> Response:
    from fieldnote.db.models import ClaimVsReported, Theme
    from fieldnote.textutil import truncate_words

    ctx = ctx_of(request)
    cfg = ctx.cfg
    with ctx.db.session() as s:
        run = repo.latest_run_with(s, cfg.workspace, Theme)
        themes = repo.themes_for_run(s, cfg.workspace, run) if run else []
        tags = repo.voice_rows(s, cfg.workspace, since=ctx.now - timedelta(days=14), until=ctx.now)
        cvr_run = repo.latest_run_with(s, cfg.workspace, ClaimVsReported)
        cvr = repo.cvr_for_run(s, cfg.workspace, cvr_run) if cvr_run else []
        docs = repo.get_documents(s, [int(i) for t in themes for i in (t.sample_document_ids or [])[:4]])
        theme_rows = [
            {
                "id": t.id,
                "label": t.label,
                "size": t.size,
                "size_prev": t.size_prev,
                "sentiment": t.sentiment_mean,
                "emerging": t.is_emerging,
                "keywords": (t.keywords or [])[:8],
                "samples": [
                    {
                        "text": truncate_words(docs[int(i)].content, 28),
                        "url": docs[int(i)].url,
                        "source": docs[int(i)].source_name,
                    }
                    for i in (t.sample_document_ids or [])[:4]
                    if int(i) in docs
                ],
            }
            for t in themes
        ]
        cells: dict[tuple[str, str], list[float]] = defaultdict(list)
        for t, _ in tags:
            for e in t.entities or ["(no brand)"]:
                cells[(t.aspect, e)].append(t.sentiment)
        cvr_rows = [
            {
                "entity": r.entity,
                "metric": r.metric,
                "unit": r.unit,
                "claimed": r.claimed_value,
                "claimed_excerpt": r.claimed_excerpt,
                "median": r.reported_median,
                "q1": r.reported_q1,
                "q3": r.reported_q3,
                "n": r.n,
                "low": "low_confidence" in (r.flags or []),
                "samples": [
                    {
                        "value": x.get("value"),
                        "excerpt": truncate_words(x.get("excerpt", ""), 25),
                        "document_id": x.get("document_id"),
                    }
                    for x in (r.reported_values or [])[:5]
                ],
            }
            for r in cvr
        ]
    heat = [{"aspect": a, "entity": e, "mean": sum(v) / len(v), "n": len(v)} for (a, e), v in sorted(cells.items())]
    return J(
        {
            "themes": theme_rows,
            "heat": heat,
            "tags_total": len(tags),
            "cvr": cvr_rows,
            "competitors": cfg.competitor_names(),
        }
    )


def findings(request: Request) -> Response:
    ctx = ctx_of(request)
    statuses = tuple(x for x in request.query_params.get("statuses", "active,demoted").split(",") if x) or ("active",)
    with ctx.db.session() as s:
        rows = [_finding(f, s) for f in repo.findings(s, ctx.cfg.workspace, statuses=statuses)]
    return J({"findings": rows, "weights": dict(ctx.cfg.scoring.weights)})


# ---- ask --------------------------------------------------------------------------------------------

_ASK: dict[tuple[str, str], Any] = {}
_ASK_LOCK = threading.Lock()


def _llm(ctx: wc.Ctx, budget: float | None = None) -> Any:
    from fieldnote.costs import CostTracker
    from fieldnote.llm.base import make_llm
    from fieldnote.llm.cache import LLMCacheStore

    return make_llm(
        get_settings(),
        offline=ctx.fixture,
        cache=LLMCacheStore(ctx.db, ctx.cfg.workspace),
        cost=CostTracker(budget_usd=budget if budget is not None else ctx.cfg.limits.max_llm_cost_per_run_usd),
    )


def ask_info(request: Request) -> Response:
    ctx = ctx_of(request)
    return J({"llm": _llm(ctx).name})


async def ask_q(request: Request) -> Response:
    data = await body(request)
    question = str(data.get("question", "")).strip()
    sid = str(data.get("session") or uuid.uuid4().hex[:12])[:40]
    if not question:
        raise ApiError("Type a question first.")
    if len(question) > 2000:
        raise ApiError("Keep the question under 2,000 characters.")

    def work() -> Response:
        from fieldnote.ask.answer import AskSession, ask

        ctx = ctx_of(request)
        with _ASK_LOCK:
            session = _ASK.setdefault((ctx.cfg.workspace, sid), AskSession())
        ans = ask(ctx.db, ctx.cfg, question, llm=_llm(ctx), session=session, now=ctx.now)
        return J(
            {
                "session": sid,
                "question": question,
                "intent": ans.intent,
                "route_via": ans.route_via,
                "sentences": [{"text": x.text, "citations": x.citations} for x in ans.sentences],
                "sources": _evidence(ans.sources),
                "missing": ans.missing,
                "markdown": ans.markdown,
            }
        )

    from starlette.concurrency import run_in_threadpool

    return await run_in_threadpool(work)


async def ask_reset(request: Request) -> Response:
    data = await body(request)
    ws = request.path_params["ws"]
    with _ASK_LOCK:
        _ASK.pop((ws, str(data.get("session", ""))), None)
    return J({"ok": True})


# ---- briefs -----------------------------------------------------------------------------------------


def briefs(request: Request) -> Response:
    from fieldnote.delivery.dispatch import active_channels

    ctx = ctx_of(request)
    kind = request.query_params.get("kind", "daily")
    with ctx.db.session() as s:
        rows = [
            {
                "id": b.id,
                "kind": b.kind,
                "run_id": b.run_id,
                "formats": sorted(k for k, v in (b.formats or {}).items() if v and Path(v).exists()),
                "question": (b.meta or {}).get("question", ""),
                "created": b.created_at,
                "delivered": b.delivered_at,
                "channel": b.channel,
            }
            for b in repo.list_briefs(s, ctx.cfg.workspace, kind=kind, limit=60)
        ]
    live, reasons = active_channels(ctx.cfg, get_settings(), dry_run=False)
    return J({"briefs": rows, "channels": live, "dry_run_reasons": reasons})


def _brief_row(ctx: wc.Ctx, brief_id: int) -> Any:
    with ctx.db.session() as s:
        for b in repo.list_briefs(s, ctx.cfg.workspace, limit=500):
            if b.id == brief_id:
                s.expunge(b)
                return b
    raise ApiError("Brief not found.", 404)


def brief_detail(request: Request) -> Response:
    ctx = ctx_of(request)
    b = _brief_row(ctx, int(request.path_params["bid"]))
    fm = b.formats or {}
    md_path = Path(fm.get("md", b.path))
    text = md_path.read_text(encoding="utf-8") if md_path.suffix == ".md" and md_path.exists() else ""
    return J({"id": b.id, "kind": b.kind, "run_id": b.run_id, "markdown": text, "created": b.created_at})


def brief_file(request: Request) -> Response:
    ctx = ctx_of(request)
    b = _brief_row(ctx, int(request.path_params["bid"]))
    fmt = request.path_params["fmt"]
    raw = (b.formats or {}).get(fmt)
    path = Path(raw) if raw else None
    if path is None or not path.exists():
        raise ApiError(f"No {fmt.upper()} file for this brief.", 404)
    mime = {"md": "text/markdown", "html": "text/html", "pdf": "application/pdf", "txt": "text/plain"}.get(
        fmt, "application/octet-stream"
    )
    return FileResponse(path, media_type=mime, filename=path.name)


def brief_send(request: Request) -> Response:
    from fieldnote.delivery.dispatch import active_channels, deliver_briefs

    ctx = ctx_of(request)
    b = _brief_row(ctx, int(request.path_params["bid"]))
    if b.kind not in ("daily", "weekly"):
        raise ApiError("Only daily and weekly briefs can be sent.")
    live, _ = active_channels(ctx.cfg, get_settings(), dry_run=False)
    res = deliver_briefs(
        ctx.db, ctx.cfg, get_settings(), b.run_id if b.kind == "daily" else None, dry_run=not live, kinds=(b.kind,)
    )
    return J({"results": res})


# ---- actions ----------------------------------------------------------------------------------------


def actions(request: Request) -> Response:
    from fieldnote.coordination.tracker import PRIORITIES, STATUSES, list_items

    ctx = ctx_of(request)
    show = request.query_params.get("status", "open")
    rows = list_items(ctx.db, ctx.cfg, None if show == "all" else (show,))
    return J({"items": rows, "statuses": STATUSES, "priorities": PRIORITIES, "today": ctx.cfg.local_date(ctx.now)})


async def action_update(request: Request) -> Response:
    data = await body(request)

    def work() -> Response:
        from fieldnote.coordination.tracker import update_item

        ctx = ctx_of(request)
        kwargs: dict[str, Any] = {}
        for field in ("description", "owner", "priority", "status"):
            if field in data:
                kwargs[field] = str(data[field])
        if "due_date" in data:
            if data["due_date"]:
                try:
                    kwargs["due_date"] = date.fromisoformat(str(data["due_date"]))
                except ValueError as exc:
                    raise ApiError("Due date must look like 2026-09-30.") from exc
            else:
                kwargs["clear_due"] = True
        return J(update_item(ctx.db, ctx.cfg, int(request.path_params["aid"]), **kwargs))

    from starlette.concurrency import run_in_threadpool

    return await run_in_threadpool(work)


def actions_csv(request: Request) -> Response:
    from fieldnote.coordination.tracker import export_csv

    ctx = ctx_of(request)
    path = export_csv(ctx.db, ctx.cfg)
    return FileResponse(path, media_type="text/csv", filename=path.name)


def reminders(request: Request) -> Response:
    from fieldnote.coordination.reminders import run_reminders

    ctx = ctx_of(request)
    res = run_reminders(ctx.db, ctx.cfg, get_settings(), dry_run=False)
    return J({"text": res.text, "deliveries": res.deliveries, "skipped": res.skipped_already_reminded})


_PREVIEWS: dict[str, Any] = {}


async def notes_preview(request: Request) -> Response:
    data = await body(request)

    def work() -> Response:
        from fieldnote.coordination.extractor import preview_note

        ctx = ctx_of(request)
        text = str(data.get("body", "")).strip()
        if not text:
            raise ApiError("Paste the meeting notes first.")
        try:
            note_date = date.fromisoformat(str(data.get("date") or ctx.cfg.local_date(ctx.now)))
        except ValueError as exc:
            raise ApiError("Meeting date must look like 2026-09-30.") from exc
        prev = preview_note(ctx.db, ctx.cfg, _llm(ctx, 0.5), text, note_date, str(data.get("title", "")).strip())
        token = uuid.uuid4().hex
        _PREVIEWS[token] = (ctx.cfg.workspace, prev)
        return J(
            {
                "token": token,
                "via": prev.via,
                "note_date": prev.note_date,
                "items": [dataclasses.asdict(i) for i in prev.items],
                "merges": [dataclasses.asdict(m) for m in prev.merges],
            }
        )

    from starlette.concurrency import run_in_threadpool

    return await run_in_threadpool(work)


async def notes_commit(request: Request) -> Response:
    data = await body(request)

    def work() -> Response:
        from fieldnote.coordination.extractor import commit_preview

        ctx = ctx_of(request)
        entry = _PREVIEWS.get(str(data.get("token", "")))
        if entry is None or entry[0] != ctx.cfg.workspace:
            raise ApiError("That preview has expired; extract the notes again.")
        selected = [int(i) for i in data.get("selected", [])]
        res = commit_preview(
            ctx.db, ctx.cfg, entry[1], accept_merges=bool(data.get("accept_merges", True)), selected=selected
        )
        _PREVIEWS.pop(str(data.get("token")), None)
        return J(res)

    from starlette.concurrency import run_in_threadpool

    return await run_in_threadpool(work)


# ---- runs and evals ---------------------------------------------------------------------------------


def runs(request: Request) -> Response:
    ctx = ctx_of(request)
    with ctx.db.session() as s:
        rows = [_run_dict(r) for r in repo.list_runs(s, ctx.cfg.workspace)]
    return J({"runs": rows})


def run_detail(request: Request) -> Response:
    ctx = ctx_of(request)
    rid = int(request.path_params["rid"])
    with ctx.db.session() as s:
        run = repo.get_run(s, rid)
        if run is None or run.workspace != ctx.cfg.workspace:
            raise ApiError("Run not found.", 404)
        traces = [
            {
                "agent": t.agent,
                "step": t.step,
                "status": t.status,
                "tool_calls": t.tool_calls or [],
                "tokens_in": t.tokens_in,
                "tokens_out": t.tokens_out,
                "cost": t.cost,
                "latency_ms": t.latency_ms,
                "error": t.error,
                "output": (t.output_summary or "")[:400],
            }
            for t in repo.traces_for_run(s, ctx.cfg.workspace, rid)
        ]
        claims = [
            {
                "id": c.id,
                "agent": c.agent,
                "type": c.type,
                "status": c.status,
                "text": c.text,
                "critic": c.critic_notes,
                "stage2": (c.checks or {}).get("stage2", ""),
            }
            for c in repo.claims_for_run(s, ctx.cfg.workspace, rid)
        ]
        out = _run_dict(run)
    return J({"run": out, "traces": traces, "claims": claims})


def evals(request: Request) -> Response:
    """Eval results. Evals score the pipeline itself on the labelled dataset (``fieldnote eval``), so when the
    live database holds none, the results recorded in the eval database are shown and labelled as such."""
    from fieldnote.config import resolve_database_url

    ctx = ctx_of(request)

    def history(db: Any) -> list[dict[str, Any]]:
        with db.session() as s:
            return [
                {"id": e.id, "created": e.created_at, "mode": e.mode, "passed": e.passed, "metrics": e.metrics or {}}
                for e in repo.eval_history(s, ctx.cfg.workspace, limit=50)
            ]

    hist, source = history(ctx.db), "live"
    if not hist and not wc.demo_mode():
        from fieldnote.config import data_dir

        if (data_dir() / "demo.db").exists():
            hist, source = history(wc._db(resolve_database_url(offline=True))), "labelled"
    report = out_dir() / "eval_report.md"
    return J(
        {"history": hist, "source": source, "report": report.read_text(encoding="utf-8") if report.exists() else ""}
    )


# ---- workspace setup --------------------------------------------------------------------------------


def config_get(request: Request) -> Response:
    from fieldnote.workspaces import get_yaml, list_entries

    ctx = ctx_of(request)
    ws = ctx.cfg.workspace
    text, source = get_yaml(ws, wc.registry())
    entry = next((e for e in list_entries(wc.registry(), include_archived=True) if e.name == ws), None)
    news = ctx.cfg.sources.news
    return J(
        {
            "yaml": text,
            "source": source,
            "has_file": bool(entry and entry.has_file),
            "has_row": bool(entry and entry.has_row),
            "entities": len(ctx.cfg.competitors),
            "news_searches": len(news.search_queries),
            "feeds": len(news.rss_feeds),
            "pages": sum(len(c.pages) for c in ctx.cfg.competitors),
        }
    )


async def config_put(request: Request) -> Response:
    require_edit()
    data = await body(request)

    def work() -> Response:
        from fieldnote.workspaces import parse_yaml_text, save

        ws = request.path_params["ws"]
        text = str(data.get("yaml", ""))
        cfg = parse_yaml_text(text, f"edit:{ws}")
        if cfg.workspace != ws:
            raise ApiError(f"The id must stay '{ws}'; to rename, create a new workspace and archive this one.")
        saved = save(text, origin="edit", db=wc.registry(), overwrite=True)
        note = "" if saved.file_path else " (database only: the workspaces folder is not writable)"
        return J({"message": f"Saved '{ws}'{note}. Run `fieldnote doctor -w {ws}` to re-check new URLs."})

    from starlette.concurrency import run_in_threadpool

    return await run_in_threadpool(work)


def run_start(request: Request) -> Response:
    from fieldnote.dashboard import runner

    require_edit()
    ws = request.path_params["ws"]
    try:
        info = runner.launch_run(wc.db_for(ws), ws)
    except runner.RunBusyError as exc:
        raise ApiError(str(exc), 409) from exc
    except OSError as exc:
        raise ApiError(f"Could not start the run: {exc}") from exc
    return J({"pid": info.pid, "log": info.log_path.name})


def run_status(request: Request) -> Response:
    from fieldnote.dashboard import runner

    ws = request.path_params["ws"]
    db = wc.db_for(ws)
    busy = runner.is_busy(db, ws)
    active = runner.active_run(db, ws)
    with db.session() as s:
        rows = [_run_dict(r) for r in repo.list_runs(s, ws, limit=8)]
    path, log = runner.latest_log(ws)
    return J(
        {"busy": busy, "active": _run_dict(active), "runs": rows, "log_name": path.name if path else None, "log": log}
    )


def all_workspaces(request: Request) -> Response:
    from fieldnote.workspaces import list_entries

    out = []
    for e in list_entries(wc.registry(), include_archived=True):
        with wc.db_for(e.name).session() as s:
            run = repo.last_run(s, e.name, kinds=("daily",), statuses=("success", "partial", "failed"))
            last = {"date": run.run_date, "status": run.status} if run else None
        out.append(
            {
                "name": e.name,
                "display_name": e.display_name,
                "storage": e.storage,
                "source": e.source,
                "status": e.status if e.valid else "invalid",
                "last_run": last,
                "problem": e.error.splitlines()[0][:200] if e.error else "",
                "description": e.description,
            }
        )
    return J({"workspaces": out})


def ws_archive(request: Request) -> Response:
    from fieldnote.workspaces import archive

    require_edit()
    archive(request.path_params["ws"], wc.registry())
    return J({"message": f"Archived '{request.path_params['ws']}'."})


def ws_restore(request: Request) -> Response:
    from fieldnote.workspaces import restore

    require_edit()
    restore(request.path_params["ws"], wc.registry())
    return J({"message": f"Restored '{request.path_params['ws']}'."})


async def ws_import(request: Request) -> Response:
    require_edit()
    data = await body(request)
    text = str(data.get("yaml", ""))
    if len(text.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise ApiError("That file is too large for a workspace config (limit 200 KB).")

    def work() -> Response:
        from fieldnote.workspaces import save

        saved = save(text, origin="import", db=wc.registry(), overwrite=bool(data.get("overwrite")))
        return J(
            {
                "name": saved.name,
                "message": f"Imported '{saved.name}'. Run `fieldnote doctor -w {saved.name}` to check its URLs.",
            }
        )

    from starlette.concurrency import run_in_threadpool

    return await run_in_threadpool(work)


# ---- builder ----------------------------------------------------------------------------------------


def _builder_llm() -> Any:
    from fieldnote.costs import CostTracker
    from fieldnote.llm.base import make_llm
    from fieldnote.llm.cache import LLMCacheStore

    return make_llm(get_settings(), cache=LLMCacheStore(wc.registry(), "_builder"), cost=CostTracker(budget_usd=0.5))


def library(request: Request) -> Response:
    from fieldnote.builder.catalog import CATEGORIES, REGIONS, TEMPLATES, search

    q = request.query_params.get("q", "")
    cat = request.query_params.get("category") or None
    region = request.query_params.get("region", "India")
    llm = _builder_llm()
    return J(
        {
            "categories": CATEGORIES,
            "regions": REGIONS,
            "total": len(TEMPLATES),
            "drafter": "rule-based drafter (no LLM key set)" if llm.is_mock else llm.name,
            "is_mock": llm.is_mock,
            "templates": [
                {
                    "id": t.id,
                    "title": t.title,
                    "category": t.category,
                    "blurb": t.blurb,
                    "players": list(t.players_for(region)),
                }
                for t in search(q, cat)
            ],
        }
    )


@dataclasses.dataclass
class _Job:
    id: str
    progress: list[str] = dataclasses.field(default_factory=list)
    state: str = "running"  # running | done | error
    error: str = ""
    result: Any = None


_JOBS: dict[str, _Job] = {}


def _draft_json(result: Any) -> dict[str, Any]:
    cfg = result.config
    src = cfg["sources"]
    return {
        "display_name": cfg["display_name"],
        "workspace": cfg["workspace"],
        "perspective": cfg["perspective"],
        "focal_company": cfg.get("focal_company") or "",
        "region": cfg["region"],
        "timezone": cfg["timezone"],
        "language_hints": cfg["language_hints"],
        "competitors": [
            {
                "name": c["name"],
                "aliases": c["aliases"],
                "pages": [{"url": p["url"], "kind": p["kind"]} for p in c["pages"]],
            }
            for c in cfg["competitors"]
        ],
        "aspects": [{"aspect": a, "words": cfg["aspect_keywords"].get(a, [])} for a in cfg["aspects"]],
        "search_queries": src["news"]["search_queries"],
        "rss_feeds": src["news"]["rss_feeds"],
        "subreddits": src["reddit"]["subreddits"],
        "reddit_terms": src["reddit"]["search_terms"],
        "youtube_terms": src["youtube"]["search_terms"],
        "checks": [{"kind": c.kind, "status": c.status, "value": c.value, "detail": c.detail} for c in result.checks],
        "warnings": result.warnings,
        "counts": result.counts(),
        "drafted_by": result.drafted_by,
        "yaml": result.yaml_text,
    }


async def builder_draft(request: Request) -> Response:
    require_edit()
    data = await body(request)
    from fieldnote.builder.catalog import get_template
    from fieldnote.builder.core import BuildRequest

    if data.get("template_id"):
        region = str(data.get("region") or "").strip()
        if not region:
            raise ApiError("Choose a region for this template.")
        try:
            tpl = get_template(str(data["template_id"]))
        except KeyError as exc:
            raise ApiError("Unknown template.") from exc
        req = BuildRequest(tpl.description(region), region)
    else:
        req = BuildRequest(
            str(data.get("description", "")),
            str(data.get("region", "")).strip() or None,
            str(data.get("perspective", "")).strip() or None,
            str(data.get("focal_company", "")).strip() or None,
            None,
            str(data.get("language", "")).strip() or None,
        )
    req.validate()
    verify = bool(data.get("verify", True))
    job = _Job(id=uuid.uuid4().hex[:12])
    _JOBS[job.id] = job

    def work() -> None:
        from fieldnote.builder.core import build_workspace
        from fieldnote.workspaces import list_entries

        llm = _builder_llm()
        try:
            taken = {e.name for e in list_entries(wc.registry(), include_archived=True)}
            job.result = build_workspace(
                req, llm=llm, settings=get_settings(), taken=taken, verify=verify, progress=job.progress.append
            )
            job.state = "done"
        except Exception as exc:
            job.error = str(exc)
            job.state = "error"
        finally:
            llm.cache.flush()

    threading.Thread(target=work, daemon=True).start()
    return J({"job": job.id, "description": req.description})


def builder_job(request: Request) -> Response:
    job = _JOBS.get(request.path_params["jid"])
    if job is None:
        raise ApiError("That draft has expired; start again.", 404)
    out: dict[str, Any] = {"state": job.state, "progress": job.progress, "error": job.error}
    if job.state == "done":
        out["draft"] = _draft_json(job.result)
    return J(out)


def _edits(data: dict[str, Any], result: Any) -> Any:
    from fieldnote.builder.core import ReviewEdits, split_csv

    original = [c["name"] for c in result.config["competitors"]]
    entities = []
    for e in data.get("entities", []):
        name = str(e.get("name", "")).strip()
        if name:
            idx = original.index(name) if name in original else None
            entities.append((name, split_csv(e.get("aliases", [])), idx))
    aspects = [
        (str(a.get("aspect", "")).strip(), split_csv(a.get("words", [])))
        for a in data.get("aspects", [])
        if str(a.get("aspect", "")).strip()
    ]

    def lst(key: str) -> list[str]:
        return [str(x).strip() for x in data.get(key, []) if str(x).strip()]

    return ReviewEdits(
        display_name=str(data.get("display_name", "")),
        workspace=str(data.get("workspace", "")).strip(),
        perspective=str(data.get("perspective", "")),
        focal_company=str(data.get("focal_company", "")),
        entities=entities,
        aspects=aspects,
        search_queries=lst("search_queries"),
        feeds=lst("rss_feeds"),
        pages=lst("pages"),
        subreddits=lst("subreddits"),
        reddit_terms=lst("reddit_terms"),
        youtube_terms=lst("youtube_terms"),
    )


def _finalized(job: _Job, data: dict[str, Any]) -> tuple[str, str]:
    from fieldnote.builder.core import BuilderError, apply_edits, finalize
    from fieldnote.workspaces import list_entries

    result = job.result
    try:
        edited = apply_edits(result.config, _edits(data, result))
        _cfg, yaml_text = finalize(
            edited,
            description=result.request.description,
            checks=result.checks,
            drafted_by=result.drafted_by,
            when=result.created_at,
        )
    except BuilderError as exc:
        return "", str(exc)
    taken = {e.name for e in list_entries(wc.registry(), include_archived=True)}
    slug = str(data.get("workspace", "")).strip()
    if slug in taken:
        return yaml_text, f"A workspace with id '{slug}' already exists; choose another id."
    return yaml_text, ""


async def builder_review(request: Request) -> Response:
    data = await body(request)
    job = _JOBS.get(request.path_params["jid"])
    if job is None or job.state != "done":
        raise ApiError("That draft has expired; start again.", 404)
    from starlette.concurrency import run_in_threadpool

    yaml_text, error = await run_in_threadpool(_finalized, job, data)
    return J({"yaml": yaml_text, "error": error})


async def builder_create(request: Request) -> Response:
    require_edit()
    data = await body(request)
    job = _JOBS.get(request.path_params["jid"])
    if job is None or job.state != "done":
        raise ApiError("That draft has expired; start again.", 404)

    def work() -> Response:
        from fieldnote.dashboard import runner
        from fieldnote.workspaces import save

        yaml_text, error = _finalized(job, data)
        if error:
            raise ApiError(f"Fix before saving: {error}")
        saved = save(yaml_text, origin="builder", description=job.result.request.description, db=wc.registry())
        msg = f"Created '{saved.name}'."
        if saved.file_error:
            msg += " Stored in the database only (the workspaces folder is not writable)."
        if data.get("run_after", True):
            try:
                runner.launch_run(wc.db_for(saved.name), saved.name)
                msg += " The first live collection is running: follow it under Workspaces."
            except (runner.RunBusyError, ConfigError, OSError) as exc:
                msg += f" Could not start the first run ({exc}); start it under Workspaces."
        _JOBS.pop(job.id, None)
        return J({"name": saved.name, "message": msg})

    from starlette.concurrency import run_in_threadpool

    return await run_in_threadpool(work)


# ---- static UI --------------------------------------------------------------------------------------


def spa(request: Request) -> Response:
    rel = request.path_params.get("path", "")
    if rel.startswith("api/"):
        return J({"error": "Not found."}, 404)
    if rel:
        candidate = (STATIC_DIR / rel).resolve()
        if STATIC_DIR in candidate.parents and candidate.is_file():
            headers = {"Cache-Control": "public, max-age=31536000, immutable"} if rel.startswith("assets/") else None
            return FileResponse(candidate, headers=headers)
    index = STATIC_DIR / "index.html"
    if not index.exists():
        return PlainTextResponse(
            "The web UI is not built yet. Run `npm install && npm run build` in the web/ folder.", status_code=503
        )
    return FileResponse(index, headers={"Cache-Control": "no-cache"})


def _r(path: str, fn: Any, methods: list[str] | None = None) -> Route:
    return Route(path, _errors(fn), methods=methods or ["GET"])


W = "/api/ws/{ws:str}"
routes = [
    _r(f"{W}/live", live_api.live_status),
    _r(f"{W}/latest", live_api.latest),
    _r(f"{W}/refresh", live_api.refresh, ["POST"]),
    Route(f"{W}/events", _errors(live_api.events)),
    _r("/api/meta", meta),
    _r("/api/login", login, ["POST"]),
    _r("/api/library", library),
    _r("/api/workspaces", all_workspaces),
    _r("/api/workspaces/import", ws_import, ["POST"]),
    _r("/api/workspaces/{ws:str}/archive", ws_archive, ["POST"]),
    _r("/api/workspaces/{ws:str}/restore", ws_restore, ["POST"]),
    _r("/api/builder/draft", builder_draft, ["POST"]),
    _r("/api/builder/jobs/{jid:str}", builder_job),
    _r("/api/builder/jobs/{jid:str}/review", builder_review, ["POST"]),
    _r("/api/builder/jobs/{jid:str}/create", builder_create, ["POST"]),
    _r(f"{W}/context", workspace_context),
    _r(f"{W}/overview", overview),
    _r(f"{W}/changes", changes),
    _r(f"{W}/metrics", metrics),
    _r(f"{W}/voice", voice),
    _r(f"{W}/findings", findings),
    _r(f"{W}/ask", ask_info),
    _r(f"{W}/ask", ask_q, ["POST"]),
    _r(f"{W}/ask/reset", ask_reset, ["POST"]),
    _r(f"{W}/briefs", briefs),
    _r(f"{W}/briefs/{{bid:int}}", brief_detail),
    _r(f"{W}/briefs/{{bid:int}}/file/{{fmt:str}}", brief_file),
    _r(f"{W}/briefs/{{bid:int}}/send", brief_send, ["POST"]),
    _r(f"{W}/actions", actions),
    _r(f"{W}/actions.csv", actions_csv),
    _r(f"{W}/actions/reminders", reminders, ["POST"]),
    _r(f"{W}/actions/{{aid:int}}", action_update, ["PATCH"]),
    _r(f"{W}/notes/preview", notes_preview, ["POST"]),
    _r(f"{W}/notes/commit", notes_commit, ["POST"]),
    _r(f"{W}/runs", runs),
    _r(f"{W}/runs/{{rid:int}}", run_detail),
    _r(f"{W}/evals", evals),
    _r(f"{W}/config", config_get),
    _r(f"{W}/config", config_put, ["PUT"]),
    _r(f"{W}/run", run_start, ["POST"]),
    _r(f"{W}/run-status", run_status),
    Route("/{path:path}", spa),
]


@contextlib.asynccontextmanager
async def lifespan(_app: Starlette) -> Any:
    """Start live collection with the server (unless demo mode or FIELDNOTE_SCHEDULER=off)."""
    from fieldnote.live.scheduler import get_scheduler, start_embedded

    if not wc.demo_mode():
        start_embedded()
    yield
    sched = get_scheduler()
    if sched is not None:
        sched.stop()


app = Starlette(routes=routes, middleware=[Middleware(AuthMiddleware)], lifespan=lifespan)


def serve(host: str = "127.0.0.1", port: int = 8501) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="warning", timeout_graceful_shutdown=3)
