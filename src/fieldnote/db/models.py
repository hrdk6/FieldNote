"""SQLAlchemy 2.0 models. Every table carries ``workspace`` and timestamps."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA_VERSION = 4


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSON, list[Any]: JSON}


def _now() -> datetime:
    from fieldnote.db.engine import utcnow

    return utcnow()


class SchemaVersion(Base):
    __tablename__ = "schema_version"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace: Mapped[str] = mapped_column(String(64), default="*")
    applied_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (UniqueConstraint("workspace", "run_date", "mode", "kind", name="uq_run_day"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace: Mapped[str] = mapped_column(String(64), index=True)
    run_date: Mapped[str] = mapped_column(String(10))
    mode: Mapped[str] = mapped_column(String(16), default="live")  # live | offline
    kind: Mapped[str] = mapped_column(String(16), default="daily")  # daily | baseline
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    as_of: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("workspace", "url", name="uq_doc_url"),
        UniqueConstraint("workspace", "content_hash", name="uq_doc_hash"),
        Index("ix_doc_ws_type", "workspace", "source_type"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace: Mapped[str] = mapped_column(String(64), index=True)
    source_type: Mapped[str] = mapped_column(String(16))  # news | page | reddit | youtube | other
    source_name: Mapped[str] = mapped_column(String(200), default="")
    url: Mapped[str] = mapped_column(String(1000))
    canonical_url: Mapped[str] = mapped_column(String(1000), default="")
    title: Mapped[str] = mapped_column(String(500), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    content: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64))
    language: Mapped[str] = mapped_column(String(16), default="en")
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    entities: Mapped[list[Any]] = mapped_column(JSON, default=list)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    first_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class PageSnapshot(Base):
    __tablename__ = "page_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    competitor: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(1000), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    text: Mapped[str] = mapped_column(Text, default="")
    structured: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    hash: Mapped[str] = mapped_column(String(64))
    document_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ChangeEvent(Base):
    __tablename__ = "change_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    competitor: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(1000))
    kind: Mapped[str] = mapped_column(String(16))  # price | feature | launch | policy | dealer | other
    before: Mapped[str] = mapped_column(Text, default="")
    after: Mapped[str] = mapped_column(Text, default="")
    significance: Mapped[float] = mapped_column(Float, default=0.0)
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prev_snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    document_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class MetricPoint(Base):
    __tablename__ = "metric_points"
    __table_args__ = (UniqueConstraint("workspace", "connector", "entity", "region", "period", name="uq_metric_point"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace: Mapped[str] = mapped_column(String(64), index=True)
    connector: Mapped[str] = mapped_column(String(200))
    entity: Mapped[str] = mapped_column(String(200))
    region: Mapped[str] = mapped_column(String(200), default="all")
    period: Mapped[str] = mapped_column(String(16))  # YYYY-MM
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(64), default="")
    source_url: Mapped[str] = mapped_column(String(1000), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class VoiceTag(Base):
    __tablename__ = "voice_tags"
    __table_args__ = (UniqueConstraint("document_id", name="uq_voice_doc"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace: Mapped[str] = mapped_column(String(64), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    entities: Mapped[list[Any]] = mapped_column(JSON, default=list)
    aspect: Mapped[str] = mapped_column(String(64), default="other")
    sentiment: Mapped[float] = mapped_column(Float, default=0.0)
    stance_quote: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str] = mapped_column(String(16), default="en")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Theme(Base):
    __tablename__ = "themes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[int] = mapped_column(Integer, index=True)
    label: Mapped[str] = mapped_column(String(300))
    keywords: Mapped[list[Any]] = mapped_column(JSON, default=list)
    size: Mapped[int] = mapped_column(Integer, default=0)
    size_prev: Mapped[int] = mapped_column(Integer, default=0)
    wow_change: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_emerging: Mapped[bool] = mapped_column(Boolean, default=False)
    sentiment_mean: Mapped[float] = mapped_column(Float, default=0.0)
    sample_document_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    aspect_distribution: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    entity_distribution: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ClaimVsReported(Base):
    __tablename__ = "claim_vs_reported"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[int] = mapped_column(Integer, index=True)
    metric: Mapped[str] = mapped_column(String(64))
    unit: Mapped[str] = mapped_column(String(32), default="")
    entity: Mapped[str] = mapped_column(String(200))
    claimed_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    claimed_source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    claimed_excerpt: Mapped[str] = mapped_column(Text, default="")
    reported_median: Mapped[float | None] = mapped_column(Float, nullable=True)
    reported_q1: Mapped[float | None] = mapped_column(Float, nullable=True)
    reported_q3: Mapped[float | None] = mapped_column(Float, nullable=True)
    reported_iqr: Mapped[float | None] = mapped_column(Float, nullable=True)
    n: Mapped[int] = mapped_column(Integer, default=0)
    flags: Mapped[list[Any]] = mapped_column(JSON, default=list)
    reported_document_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    reported_values: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Claim(Base):
    __tablename__ = "claims"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    agent: Mapped[str] = mapped_column(String(32), default="researcher")
    type: Mapped[str] = mapped_column(String(16))  # fact | analysis | estimate
    text: Mapped[str] = mapped_column(Text)
    source_document_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    excerpt: Mapped[str] = mapped_column(Text, default="")
    depends_on_claim_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="unverified")  # verified | rejected | unverified
    critic_notes: Mapped[str] = mapped_column(Text, default="")
    checks: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    entities: Mapped[list[Any]] = mapped_column(JSON, default=list)
    tags: Mapped[list[Any]] = mapped_column(JSON, default=list)
    aspect: Mapped[str | None] = mapped_column(String(64), nullable=True)
    derivation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ref: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Finding(Base):
    __tablename__ = "findings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    first_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(String(300))
    category: Mapped[str] = mapped_column(String(16))  # opportunity | risk
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | demoted | stale | dropped
    what_happened: Mapped[str] = mapped_column(Text, default="")
    why_it_matters: Mapped[str] = mapped_column(Text, default="")
    evidence_claim_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    analysis_claim_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    recommended_action: Mapped[str] = mapped_column(Text, default="")
    how_to_execute: Mapped[str] = mapped_column(Text, default="")
    raw_ratings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    scores: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    total_score: Mapped[float] = mapped_column(Float, default=0.0)
    evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    entities: Mapped[list[Any]] = mapped_column(JSON, default=list)
    checks: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    prose: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=_now)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=_now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Brief(Base):
    __tablename__ = "briefs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(16))  # daily | weekly | ask
    path: Mapped[str] = mapped_column(String(1000))
    formats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    channel: Mapped[str | None] = mapped_column(String(64), nullable=True)


class MeetingNote(Base):
    __tablename__ = "meeting_notes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    body: Mapped[str] = mapped_column(Text)
    note_date: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ActionItem(Base):
    __tablename__ = "action_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace: Mapped[str] = mapped_column(String(64), index=True)
    note_id: Mapped[int | None] = mapped_column(ForeignKey("meeting_notes.id", ondelete="SET NULL"), nullable=True)
    description: Mapped[str] = mapped_column(Text)
    owner: Mapped[str] = mapped_column(String(200), default="Unassigned")
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    priority: Mapped[str] = mapped_column(String(16), default="medium")  # low | medium | high
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | done | dropped
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    source_sentence: Mapped[str] = mapped_column(Text, default="")
    flags: Mapped[list[Any]] = mapped_column(JSON, default=list)
    history: Mapped[list[Any]] = mapped_column(JSON, default=list)
    last_reminded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class LLMCache(Base):
    __tablename__ = "llm_cache"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace: Mapped[str] = mapped_column(String(64), default="*")
    task: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(100))
    response: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class AgentTrace(Base):
    __tablename__ = "agent_traces"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    agent: Mapped[str] = mapped_column(String(32))
    step: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(16), default="ok")
    input_summary: Mapped[str] = mapped_column(Text, default="")
    tool_calls: Mapped[list[Any]] = mapped_column(JSON, default=list)
    output_summary: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class HttpCache(Base):
    __tablename__ = "http_cache"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace: Mapped[str] = mapped_column(String(64), default="*")
    url: Mapped[str] = mapped_column(String(1000), unique=True)
    etag: Mapped[str | None] = mapped_column(String(300), nullable=True)
    last_modified: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[int] = mapped_column(Integer, default=0)
    body: Mapped[str] = mapped_column(Text, default="")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class EvalResult(Base):
    __tablename__ = "eval_results"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mode: Mapped[str] = mapped_column(String(16), default="mock")
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    report_path: Mapped[str] = mapped_column(String(1000), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class WorkspaceConfigRow(Base):
    """Workspace YAML stored in the database so hosted dashboards and schedulers share it.

    ``workspaces/*.yaml`` files remain the primary format for local use; see ``fieldnote.workspaces``
    for how file and database copies are reconciled (the most recently changed copy wins).
    """

    __tablename__ = "workspace_configs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    yaml_text: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    origin: Mapped[str] = mapped_column(String(32), default="builder")  # builder | import | edit
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | archived
    # SHA-256 of the workspaces/<slug>.yaml content this row supersedes (None if there was no file).
    base_file_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
