"""Live endpoints: refresh status, the newest-items feed, manual refresh, and a Server-Sent Events stream.

The event stream watches the database, not the scheduler, so it reports new data whichever process wrote
it (the embedded scheduler, a ``fieldnote live`` worker, a cron job or someone running the CLI by hand).
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from fieldnote.db.engine import Database, utcnow
from fieldnote.db.models import ActionItem, ChangeEvent, Document, Finding, Run
from fieldnote.textutil import truncate_words
from fieldnote.web import context as wc

POLL_SECONDS = 3
PING_EVERY = 5  # polls between keep-alive comments when nothing changed
STREAM_SECONDS = 600  # streams end after this long; EventSource reconnects on its own (bounds any missed disconnect)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# "2026-09-25 19:13:26,429 INFO    [ws run=4] fieldnote.module: message" -> "message"
_TEXT_LOG = re.compile(r"^\d{4}-\d\d-\d\d[ T][\d:,.]+\s+\w+\s+\[[^\]]*\]\s+[\w.]+:\s*(.*)$")


def _run_brief(r: Run | None) -> dict[str, Any] | None:
    if r is None:
        return None
    stats = r.stats or {}
    return {
        "id": r.id,
        "kind": "analysis" if r.kind == "daily" else r.kind,
        "status": r.status,
        "started_at": r.started_at,
        "finished_at": r.finished_at,
        "documents_new": stats.get("documents_new"),
        "changes": (stats.get("pages") or {}).get("changes"),
        "cost_usd": r.cost_usd or 0.0,
    }


def _log_line(workspace: str) -> str:
    """The last meaningful line of the newest run log, for a one-line progress read-out."""
    from fieldnote.dashboard import runner

    _path, text = runner.latest_log(workspace, max_chars=4000)
    for line in reversed(text.splitlines()):
        line = _ANSI.sub("", line).strip()
        if not line or line.startswith(("Traceback", "File ")):
            continue
        try:  # JSON log format: show the message only
            data = json.loads(line)
            line = str(data.get("message") or data.get("msg") or line)
        except (ValueError, AttributeError):
            m = _TEXT_LOG.match(line)
            if m:
                line = m.group(1)
        return line[:180]
    return ""


def status_payload(db: Database, workspace: str) -> dict[str, Any]:
    from fieldnote.dashboard import runner
    from fieldnote.live.scheduler import LiveConfig, get_scheduler, schedule

    live = LiveConfig.from_env()
    now = utcnow()
    sched = schedule(db, workspace, live, now)
    active = runner.active_run(db, workspace)
    with db.session() as s:
        last_any = sched["last_pulse"]
        src_run = s.get(Run, last_any.id) if last_any is not None else None
        collectors = dict(((src_run.stats or {}).get("collectors") or {}) if src_run else {})
        docs = s.scalar(select(func.count(Document.id)).where(Document.workspace == workspace)) or 0
        docs_24h = (
            s.scalar(
                select(func.count(Document.id)).where(
                    Document.workspace == workspace, Document.fetched_at >= now - timedelta(hours=24)
                )
            )
            or 0
        )
        changes_24h = (
            s.scalar(
                select(func.count(ChangeEvent.id)).where(
                    ChangeEvent.workspace == workspace, ChangeEvent.detected_at >= now - timedelta(hours=24)
                )
            )
            or 0
        )
    updated = max(
        (r.finished_at for r in (sched["last_pulse"], sched["last_analysis"]) if r is not None and r.finished_at),
        default=None,
    )
    scheduler = get_scheduler()
    if wc.demo_mode():
        health = "demo"
    elif active is not None:
        health = "collecting"
    elif updated is not None and now - updated <= timedelta(minutes=live.pulse_minutes * 2 + 10):
        health = "live"
    elif updated is None:
        health = "starting" if live.enabled else "waiting"
    else:
        health = "stale"
    return {
        "health": health,
        "now": now,
        "updated_at": updated,
        "scheduler": {
            **live.as_dict(),
            "in_process": bool(scheduler and scheduler.running),
            "last_tick": scheduler.last_tick if scheduler else None,
            "last_error": scheduler.last_error if scheduler else "",
        },
        "last_pulse": _run_brief(sched["last_pulse"]),
        "last_analysis": _run_brief(sched["last_analysis"]),
        "next_pulse": sched["next_pulse"],
        "next_analysis": sched["next_analysis"],
        "active": ({**(_run_brief(active) or {}), "log_line": _log_line(workspace)} if active is not None else None),
        "sources": [
            {
                "name": name,
                "status": v.get("status", ""),
                "message": v.get("message", ""),
                "items": v.get("items", v.get("fetched", v.get("posts"))),
            }
            for name, v in collectors.items()
        ],
        "totals": {"documents": docs, "documents_24h": docs_24h, "changes_24h": changes_24h},
    }


def _fingerprint(db: Database, workspace: str) -> dict[str, Any]:
    """Cheap watermarks that move whenever anything a page shows changes, plus the active run."""
    from fieldnote.dashboard import runner

    with db.session() as s:
        version = {
            "documents": s.scalar(select(func.max(Document.id)).where(Document.workspace == workspace)) or 0,
            "changes": s.scalar(select(func.max(ChangeEvent.id)).where(ChangeEvent.workspace == workspace)) or 0,
            "runs": s.scalar(select(func.max(Run.id)).where(Run.workspace == workspace, Run.finished_at.is_not(None)))
            or 0,
            "finished": s.scalar(select(func.max(Run.finished_at)).where(Run.workspace == workspace)),
            "findings": s.scalar(select(func.max(Finding.updated_at)).where(Finding.workspace == workspace)),
            "actions": s.scalar(select(func.max(ActionItem.id)).where(ActionItem.workspace == workspace)) or 0,
        }
    active = runner.active_run(db, workspace)
    return {
        "version": json.dumps(version, default=str, sort_keys=True),
        "documents": version["documents"],
        "changes": version["changes"],
        "active": (
            {
                "id": active.id,
                "kind": "analysis" if active.kind == "daily" else active.kind,
                "started_at": active.started_at,
                "log_line": _log_line(workspace),
            }
            if active is not None
            else None
        ),
    }


# ---- handlers (wrapped by server._errors) ---------------------------------------------------------------


def live_status(request: Request) -> Response:
    from fieldnote.web.server import J, ctx_of

    ctx = ctx_of(request)
    return J(status_payload(ctx.db, ctx.cfg.workspace))


def latest(request: Request) -> Response:
    """The newest collected items (news, posts, videos) and page changes, newest first."""
    from fieldnote.web.server import J, ctx_of

    ctx = ctx_of(request)
    ws = ctx.cfg.workspace
    limit = max(5, min(int(request.query_params.get("limit", "40")), 200))
    tracked = request.query_params.get("tracked") == "1"
    when = func.coalesce(Document.published_at, Document.fetched_at)
    with ctx.db.session() as s:
        docs = s.scalars(
            select(Document)
            .where(Document.workspace == ws, Document.source_type != "page")
            .order_by(when.desc(), Document.id.desc())
            .limit(limit * 6 if tracked else limit)
        ).all()
        tagged_total = sum(1 for d in docs if d.entities)
        if tracked:
            docs = [d for d in docs if d.entities][:limit]
        changes = s.scalars(
            select(ChangeEvent)
            .where(ChangeEvent.workspace == ws)
            .order_by(ChangeEvent.detected_at.desc(), ChangeEvent.id.desc())
            .limit(max(5, limit // 4))
        ).all()
        items = [
            {
                "key": f"d{d.id}",
                "id": d.id,
                "type": "document",
                "source_type": d.source_type,
                "title": d.title or truncate_words(d.content or "", 14),
                "summary": truncate_words(d.summary or d.content or "", 36),
                "source": d.source_name,
                "url": d.url,
                "at": d.published_at or d.fetched_at,
                "fetched_at": d.fetched_at,
                "entities": [str(e) for e in (d.entities or [])][:4],
            }
            for d in docs
        ] + [
            {
                "key": f"c{c.id}",
                "id": c.id,
                "type": "change",
                "source_type": "page",
                "title": c.summary,
                "summary": "",
                "source": c.competitor,
                "url": c.url,
                "at": c.detected_at,
                "fetched_at": c.detected_at,
                "entities": [c.competitor],
                "kind": c.kind,
                "significance": c.significance,
            }
            for c in changes
        ]
    items.sort(key=lambda x: x["at"] or utcnow(), reverse=True)
    return J({"items": items[:limit], "tracked": tracked, "tagged_in_window": tagged_total, "now": utcnow()})


async def refresh(request: Request) -> Response:
    from fieldnote.dashboard import runner
    from fieldnote.web.server import ApiError, J, body, require_edit

    require_edit()
    if wc.demo_mode():
        raise ApiError("The dashboard is showing demo data; restart it without --demo to collect live data.")
    data = await body(request)
    kind = str(data.get("kind", "pulse"))
    if kind not in ("pulse", "analysis"):
        raise ApiError("kind must be 'pulse' or 'analysis'.")
    ws = request.path_params["ws"]

    def start() -> Response:
        try:
            info = runner.launch_run(wc.db_for(ws), ws, kind=kind)
        except runner.RunBusyError as exc:
            raise ApiError(str(exc), 409) from exc
        except OSError as exc:
            raise ApiError(f"Could not start the run: {exc}") from exc
        return J({"started": kind, "pid": info.pid})

    return await run_in_threadpool(start)


async def events(request: Request) -> Response:
    """Server-Sent Events: a ``state`` event whenever the data or the active run changes."""
    from fieldnote.web.server import ApiError, _default

    ws = request.path_params["ws"]
    try:
        db = await run_in_threadpool(wc.db_for, ws)
    except Exception as exc:
        raise ApiError(str(exc), 404) from exc

    async def stream() -> Any:
        yield "retry: 5000\n\n"
        last = None
        quiet = 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + STREAM_SECONDS
        while loop.time() < deadline and not await request.is_disconnected():
            try:
                state = await run_in_threadpool(_fingerprint, db, ws)
            except Exception:  # a locked or briefly unavailable database: try again next poll
                state = None
            if state is not None and state != last:
                yield f"event: state\ndata: {json.dumps(state, default=_default)}\n\n"
                last = state
                quiet = 0
            else:
                quiet += 1
                if quiet % PING_EVERY == 0:
                    yield ": ping\n\n"
            await asyncio.sleep(POLL_SECONDS)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
