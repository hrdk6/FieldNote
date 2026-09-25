"""Repository functions. All queries are parameterised through the SQLAlchemy ORM / Core."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from fieldnote.db.engine import utcnow
from fieldnote.db.models import (
    ActionItem,
    AgentTrace,
    Brief,
    ChangeEvent,
    Claim,
    ClaimVsReported,
    Document,
    EvalResult,
    Finding,
    MeetingNote,
    MetricPoint,
    PageSnapshot,
    Run,
    Theme,
    VoiceTag,
)
from fieldnote.domain import SOCIAL_SOURCE_TYPES, CollectedDocument
from fieldnote.textutil import canonical_url, content_hash

# ---------------------------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------------------------


def start_run(
    s: Session,
    workspace: str,
    run_date: str,
    *,
    mode: str = "live",
    kind: str = "daily",
    as_of: datetime | None = None,
    reset: bool = True,
) -> Run:
    """Create the run for (workspace, day, mode, kind) or reuse it if it already exists (idempotent).

    With ``reset`` (a full rerun) the run's derived rows are cleared first so they are rebuilt rather than
    duplicated; without it (e.g. ``fieldnote process`` after ``fieldnote collect``) the run continues.
    """
    run = find_run(s, workspace, run_date, mode, kind)
    if run is None:
        run = Run(workspace=workspace, run_date=run_date, mode=mode, kind=kind)
        s.add(run)
    elif reset:
        clear_run_outputs(s, workspace, run.id)
    else:
        prior_stats = dict(run.stats or {})
        run.started_at = run.started_at or utcnow()
        run.status = "running"
        run.as_of = as_of or run.as_of
        run.stats = prior_stats
        s.flush()
        return run
    run.started_at = utcnow()
    run.finished_at = None
    run.status = "running"
    run.stats = {"rerun": run.id is not None}
    run.cost_usd = 0.0
    run.tokens_in = 0
    run.tokens_out = 0
    run.as_of = as_of
    s.flush()
    return run


def find_run(s: Session, workspace: str, run_date: str, mode: str, kind: str) -> Run | None:
    """The run for (workspace, day, mode, kind), if one exists (the most recent, for kinds that repeat)."""
    return s.scalar(
        select(Run)
        .where(Run.workspace == workspace, Run.run_date == run_date, Run.mode == mode, Run.kind == kind)
        .order_by(Run.id.desc())
        .limit(1)
    )


def clear_run_outputs(s: Session, workspace: str, run_id: int) -> None:
    """Remove run-scoped derived rows so a same-day rerun updates instead of duplicating."""
    for model in (Claim, Theme, ClaimVsReported, AgentTrace, ChangeEvent, PageSnapshot):
        s.execute(delete(model).where(model.workspace == workspace, model.run_id == run_id))


def finish_run(
    s: Session,
    run: Run,
    *,
    status: str,
    stats: dict[str, Any],
    cost_usd: float,
    tokens_in: int,
    tokens_out: int,
) -> None:
    run.status = status
    run.stats = stats
    run.cost_usd = cost_usd
    run.tokens_in = tokens_in
    run.tokens_out = tokens_out
    run.finished_at = utcnow()


def get_run(s: Session, run_id: int) -> Run | None:
    return s.get(Run, run_id)


def last_run(
    s: Session,
    workspace: str,
    *,
    kinds: Sequence[str] = ("daily",),
    statuses: Sequence[str] = ("success", "partial"),
    mode: str | None = None,
    before_as_of: datetime | None = None,
    exclude_id: int | None = None,
) -> Run | None:
    q = select(Run).where(Run.workspace == workspace, Run.kind.in_(list(kinds)), Run.status.in_(list(statuses)))
    if mode:
        q = q.where(Run.mode == mode)
    if before_as_of is not None:
        q = q.where(Run.as_of < before_as_of)
    if exclude_id is not None:
        q = q.where(Run.id != exclude_id)
    return s.scalar(q.order_by(Run.as_of.desc().nulls_last(), Run.started_at.desc(), Run.id.desc()).limit(1))


def list_runs(s: Session, workspace: str, limit: int = 50) -> list[Run]:
    return list(s.scalars(select(Run).where(Run.workspace == workspace).order_by(Run.id.desc()).limit(limit)))


# ---------------------------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------------------------


def find_document(s: Session, workspace: str, *, url: str | None = None, chash: str | None = None) -> Document | None:
    conds = []
    if url:
        conds.append(Document.url == url)
    if chash:
        conds.append(Document.content_hash == chash)
    if not conds:
        return None
    return s.scalar(select(Document).where(Document.workspace == workspace, or_(*conds)).limit(1))


def upsert_document(
    s: Session, workspace: str, doc: CollectedDocument, run_id: int | None = None
) -> tuple[Document, bool]:
    """Insert a document or update the existing row with the same URL/content hash.

    Returns (document, created).
    """
    body = doc.content or doc.summary or doc.title
    chash = content_hash(f"{doc.title}\n{body}") if body else content_hash(doc.url)
    url = doc.url
    existing = find_document(s, workspace, url=url, chash=chash)
    if existing is not None:
        # Merge syndication sources and refresh mutable fields without clobbering good data.
        meta = dict(existing.meta or {})
        sources = set(meta.get("also_seen_at", []))
        if existing.url != url:
            sources.add(url)
        if sources:
            meta["also_seen_at"] = sorted(sources)
        for k, v in doc.metadata.items():
            meta.setdefault(k, v)
        existing.meta = meta
        if doc.content and len(doc.content) > len(existing.content or ""):
            new_hash = chash
            clash = s.scalar(
                select(Document.id).where(
                    Document.workspace == workspace, Document.content_hash == new_hash, Document.id != existing.id
                )
            )
            if clash is None:
                existing.content = doc.content
                existing.content_hash = new_hash
        if doc.published_at and not existing.published_at:
            existing.published_at = doc.published_at
        existing.fetched_at = doc.fetched_at or utcnow()
        return existing, False
    row = Document(
        workspace=workspace,
        source_type=doc.source_type,
        source_name=doc.source_name[:200],
        url=url[:1000],
        canonical_url=(doc.canonical_url or canonical_url(url))[:1000],
        title=(doc.title or "")[:500],
        summary=doc.summary or "",
        published_at=doc.published_at,
        fetched_at=doc.fetched_at or utcnow(),
        content=doc.content or "",
        content_hash=chash,
        language=doc.language or "en",
        is_primary=doc.is_primary,
        entities=list(doc.metadata.get("entities", [])),
        meta={k: v for k, v in doc.metadata.items() if k != "entities"},
        first_run_id=run_id,
    )
    s.add(row)
    s.flush()
    return row, True


def get_documents(s: Session, ids: Iterable[int]) -> dict[int, Document]:
    ids = [int(i) for i in ids]
    if not ids:
        return {}
    return {d.id: d for d in s.scalars(select(Document).where(Document.id.in_(ids)))}


def get_document(s: Session, doc_id: int) -> Document | None:
    return s.get(Document, int(doc_id))


def documents_between(
    s: Session,
    workspace: str,
    since: datetime | None,
    until: datetime | None = None,
    source_types: Sequence[str] | None = None,
) -> list[Document]:
    ts = func.coalesce(Document.published_at, Document.fetched_at)
    q = select(Document).where(Document.workspace == workspace)
    if since is not None:
        q = q.where(ts > since)
    if until is not None:
        q = q.where(ts <= until)
    if source_types:
        q = q.where(Document.source_type.in_(list(source_types)))
    return list(s.scalars(q.order_by(ts.desc(), Document.id.desc())))


def documents_for_run(s: Session, workspace: str, run_id: int) -> list[Document]:
    return list(s.scalars(select(Document).where(Document.workspace == workspace, Document.first_run_id == run_id)))


def all_documents(s: Session, workspace: str, until: datetime | None = None) -> list[Document]:
    q = select(Document).where(Document.workspace == workspace)
    if until is not None:
        q = q.where(func.coalesce(Document.published_at, Document.fetched_at) <= until)
    return list(s.scalars(q.order_by(Document.id)))


def count_documents(s: Session, workspace: str) -> int:
    return int(s.scalar(select(func.count(Document.id)).where(Document.workspace == workspace)) or 0)


def purge_documents(s: Session, workspace: str, now: datetime, posts_days: int, documents_days: int) -> dict[str, int]:
    ts = func.coalesce(Document.published_at, Document.fetched_at)
    post_cutoff = now - timedelta(days=posts_days)
    doc_cutoff = now - timedelta(days=documents_days)
    post_ids = list(
        s.scalars(
            select(Document.id).where(
                Document.workspace == workspace, Document.source_type.in_(SOCIAL_SOURCE_TYPES), ts < post_cutoff
            )
        )
    )
    other_ids = list(
        s.scalars(
            select(Document.id).where(
                Document.workspace == workspace, Document.source_type.notin_(SOCIAL_SOURCE_TYPES), ts < doc_cutoff
            )
        )
    )
    ids = post_ids + other_ids
    if ids:
        s.execute(delete(VoiceTag).where(VoiceTag.document_id.in_(ids)))
        s.execute(delete(Document).where(Document.id.in_(ids)))
    return {"posts": len(post_ids), "documents": len(other_ids)}


# ---------------------------------------------------------------------------------------------
# Page snapshots and change events
# ---------------------------------------------------------------------------------------------


def last_snapshot(s: Session, workspace: str, url: str, before: datetime | None = None) -> PageSnapshot | None:
    q = select(PageSnapshot).where(PageSnapshot.workspace == workspace, PageSnapshot.url == url)
    if before is not None:
        q = q.where(PageSnapshot.fetched_at < before)
    return s.scalar(q.order_by(PageSnapshot.fetched_at.desc(), PageSnapshot.id.desc()).limit(1))


def changes_between(
    s: Session,
    workspace: str,
    since: datetime | None,
    until: datetime | None = None,
    competitor: str | None = None,
    kind: str | None = None,
) -> list[ChangeEvent]:
    q = select(ChangeEvent).where(ChangeEvent.workspace == workspace)
    if since is not None:
        q = q.where(ChangeEvent.detected_at > since)
    if until is not None:
        q = q.where(ChangeEvent.detected_at <= until)
    if competitor:
        q = q.where(func.lower(ChangeEvent.competitor) == competitor.lower())
    if kind:
        q = q.where(ChangeEvent.kind == kind)
    return list(s.scalars(q.order_by(ChangeEvent.significance.desc(), ChangeEvent.detected_at.desc())))


def changes_for_run(s: Session, workspace: str, run_id: int) -> list[ChangeEvent]:
    return list(
        s.scalars(
            select(ChangeEvent)
            .where(ChangeEvent.workspace == workspace, ChangeEvent.run_id == run_id)
            .order_by(ChangeEvent.significance.desc(), ChangeEvent.id)
        )
    )


# ---------------------------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------------------------


def upsert_metric_point(
    s: Session,
    workspace: str,
    *,
    connector: str,
    entity: str,
    region: str,
    period: str,
    value: float,
    unit: str,
    source_url: str,
) -> bool:
    row = s.scalar(
        select(MetricPoint).where(
            MetricPoint.workspace == workspace,
            MetricPoint.connector == connector,
            MetricPoint.entity == entity,
            MetricPoint.region == region,
            MetricPoint.period == period,
        )
    )
    if row is None:
        s.add(
            MetricPoint(
                workspace=workspace,
                connector=connector,
                entity=entity,
                region=region,
                period=period,
                value=value,
                unit=unit,
                source_url=source_url,
            )
        )
        return True
    row.value = value
    row.unit = unit
    row.source_url = source_url
    return False


def metric_points(s: Session, workspace: str, connector: str | None = None) -> list[MetricPoint]:
    q = select(MetricPoint).where(MetricPoint.workspace == workspace)
    if connector:
        q = q.where(MetricPoint.connector == connector)
    return list(s.scalars(q.order_by(MetricPoint.period, MetricPoint.entity, MetricPoint.region)))


# ---------------------------------------------------------------------------------------------
# Voice tags, themes, claim-vs-reported
# ---------------------------------------------------------------------------------------------


def untagged_social_documents(s: Session, workspace: str, until: datetime | None = None) -> list[Document]:
    tagged = select(VoiceTag.document_id).where(VoiceTag.workspace == workspace)
    q = select(Document).where(
        Document.workspace == workspace,
        Document.source_type.in_(SOCIAL_SOURCE_TYPES),
        Document.id.notin_(tagged),
    )
    if until is not None:
        q = q.where(func.coalesce(Document.published_at, Document.fetched_at) <= until)
    return list(s.scalars(q.order_by(Document.id)))


def voice_rows(
    s: Session, workspace: str, since: datetime | None = None, until: datetime | None = None
) -> list[tuple[VoiceTag, Document]]:
    ts = func.coalesce(Document.published_at, Document.fetched_at)
    q = (
        select(VoiceTag, Document)
        .join(Document, Document.id == VoiceTag.document_id)
        .where(VoiceTag.workspace == workspace)
    )
    if since is not None:
        q = q.where(ts > since)
    if until is not None:
        q = q.where(ts <= until)
    return [(t, d) for t, d in s.execute(q.order_by(Document.id)).all()]


def themes_for_run(s: Session, workspace: str, run_id: int) -> list[Theme]:
    return list(
        s.scalars(select(Theme).where(Theme.workspace == workspace, Theme.run_id == run_id).order_by(Theme.size.desc()))
    )


def cvr_for_run(s: Session, workspace: str, run_id: int) -> list[ClaimVsReported]:
    return list(
        s.scalars(
            select(ClaimVsReported)
            .where(ClaimVsReported.workspace == workspace, ClaimVsReported.run_id == run_id)
            .order_by(ClaimVsReported.metric, ClaimVsReported.n.desc())
        )
    )


def latest_run_with(s: Session, workspace: str, model: Any) -> int | None:
    return s.scalar(select(func.max(model.run_id)).where(model.workspace == workspace))


# ---------------------------------------------------------------------------------------------
# Claims and findings
# ---------------------------------------------------------------------------------------------


def claims_for_run(
    s: Session, workspace: str, run_id: int, statuses: Sequence[str] | None = None, types: Sequence[str] | None = None
) -> list[Claim]:
    q = select(Claim).where(Claim.workspace == workspace, Claim.run_id == run_id)
    if statuses:
        q = q.where(Claim.status.in_(list(statuses)))
    if types:
        q = q.where(Claim.type.in_(list(types)))
    return list(s.scalars(q.order_by(Claim.id)))


def get_claims(s: Session, ids: Iterable[int]) -> dict[int, Claim]:
    ids = [int(i) for i in ids]
    if not ids:
        return {}
    return {c.id: c for c in s.scalars(select(Claim).where(Claim.id.in_(ids)))}


def all_claims(s: Session, workspace: str) -> list[Claim]:
    return list(s.scalars(select(Claim).where(Claim.workspace == workspace).order_by(Claim.id)))


def findings(
    s: Session, workspace: str, statuses: Sequence[str] = ("active", "demoted"), run_id: int | None = None
) -> list[Finding]:
    q = select(Finding).where(Finding.workspace == workspace, Finding.status.in_(list(statuses)))
    if run_id is not None:
        q = q.where(Finding.run_id == run_id)
    return list(s.scalars(q.order_by(Finding.total_score.desc(), Finding.id)))


def mark_stale_findings(s: Session, workspace: str, now: datetime, stale_after_days: int) -> int:
    cutoff = now - timedelta(days=stale_after_days)
    rows = list(
        s.scalars(
            select(Finding).where(
                Finding.workspace == workspace, Finding.status.in_(["active", "demoted"]), Finding.last_seen < cutoff
            )
        )
    )
    for f in rows:
        f.status = "stale"
    return len(rows)


# ---------------------------------------------------------------------------------------------
# Briefs, traces, evals
# ---------------------------------------------------------------------------------------------


def upsert_brief(
    s: Session,
    workspace: str,
    *,
    kind: str,
    path: str,
    run_id: int | None,
    formats: dict[str, str],
    meta: dict[str, Any] | None = None,
) -> Brief:
    row = None
    if kind in ("daily", "weekly") and run_id is not None:
        row = s.scalar(select(Brief).where(Brief.workspace == workspace, Brief.kind == kind, Brief.run_id == run_id))
    if row is None:
        row = Brief(workspace=workspace, kind=kind, run_id=run_id, path=path)
        s.add(row)
    row.path = path
    row.formats = formats
    row.meta = meta or {}
    s.flush()
    return row


def list_briefs(s: Session, workspace: str, kind: str | None = None, limit: int = 100) -> list[Brief]:
    q = select(Brief).where(Brief.workspace == workspace)
    if kind:
        q = q.where(Brief.kind == kind)
    return list(s.scalars(q.order_by(Brief.created_at.desc(), Brief.id.desc()).limit(limit)))


def latest_brief(s: Session, workspace: str, kind: str) -> Brief | None:
    return s.scalar(
        select(Brief).where(Brief.workspace == workspace, Brief.kind == kind).order_by(Brief.id.desc()).limit(1)
    )


def traces_for_run(s: Session, workspace: str, run_id: int) -> list[AgentTrace]:
    return list(
        s.scalars(
            select(AgentTrace)
            .where(AgentTrace.workspace == workspace, AgentTrace.run_id == run_id)
            .order_by(AgentTrace.id)
        )
    )


def eval_history(s: Session, workspace: str, limit: int = 30) -> list[EvalResult]:
    return list(
        s.scalars(
            select(EvalResult).where(EvalResult.workspace == workspace).order_by(EvalResult.id.desc()).limit(limit)
        )
    )


# ---------------------------------------------------------------------------------------------
# Meeting notes and action items
# ---------------------------------------------------------------------------------------------


def add_note(s: Session, workspace: str, title: str, body: str, note_date: date) -> MeetingNote:
    note = MeetingNote(workspace=workspace, title=title[:300], body=body, note_date=note_date)
    s.add(note)
    s.flush()
    return note


def open_action_items(s: Session, workspace: str) -> list[ActionItem]:
    return list(
        s.scalars(
            select(ActionItem)
            .where(ActionItem.workspace == workspace, ActionItem.status == "open")
            .order_by(ActionItem.due_date.is_(None), ActionItem.due_date, ActionItem.id)
        )
    )


def action_items(s: Session, workspace: str, statuses: Sequence[str] | None = None) -> list[ActionItem]:
    q = select(ActionItem).where(ActionItem.workspace == workspace)
    if statuses:
        q = q.where(ActionItem.status.in_(list(statuses)))
    return list(
        s.scalars(q.order_by(ActionItem.status, ActionItem.due_date.is_(None), ActionItem.due_date, ActionItem.id))
    )


def get_action_item(s: Session, workspace: str, item_id: int) -> ActionItem | None:
    return s.scalar(select(ActionItem).where(ActionItem.workspace == workspace, ActionItem.id == item_id))


def items_due(s: Session, workspace: str, until: date) -> list[ActionItem]:
    return list(
        s.scalars(
            select(ActionItem)
            .where(
                and_(
                    ActionItem.workspace == workspace,
                    ActionItem.status == "open",
                    ActionItem.due_date.is_not(None),
                    ActionItem.due_date <= until,
                )
            )
            .order_by(ActionItem.due_date, ActionItem.id)
        )
    )
