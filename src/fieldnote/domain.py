"""Plain data objects shared between layers (collectors, processing, pipeline)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

SOURCE_TYPES = ("news", "page", "reddit", "youtube", "other")
SOCIAL_SOURCE_TYPES = ("reddit", "youtube")


@dataclass
class CollectedDocument:
    """A document as produced by a collector, before it is stored."""

    source_type: str
    source_name: str
    url: str
    title: str = ""
    content: str = ""
    summary: str = ""
    published_at: datetime | None = None
    fetched_at: datetime | None = None
    language: str = "en"
    is_primary: bool = False
    canonical_url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def text_for_analysis(self) -> str:
        parts = [self.title, self.summary if self.summary not in self.content else "", self.content]
        return "\n".join(p for p in parts if p)


@dataclass
class CollectorResult:
    name: str
    documents: list[CollectedDocument] = field(default_factory=list)
    status: str = "ok"  # ok | skipped | failed | partial
    message: str = ""
    stats: dict[str, Any] = field(default_factory=dict)


@dataclass
class StageRecord:
    name: str
    status: str = "ok"  # ok | skipped | degraded | failed
    seconds: float = 0.0
    count: int | None = None
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "seconds": round(self.seconds, 2),
            "count": self.count,
            "message": self.message,
        }
