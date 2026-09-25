"""Offline collectors that replay the bundled FIXTURE datasets (no network, no keys).

Fixture datasets live in ``fixtures/<workspace>/``. They use fictional brands and ``.example``
URLs so demo output can never be mistaken for real market information. A manifest defines one or
more *rounds* (as-of timestamps); the offline pipeline runs them in order so that change detection,
week-over-week themes and finding persistence all have history to work with.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dateutil import parser as dtparser

from fieldnote.collectors.base import Collector
from fieldnote.config import ConfigError, Settings, WorkspaceConfig, fixtures_dir
from fieldnote.domain import CollectedDocument, CollectorResult
from fieldnote.processing.clean import extract_main_text, sanitize_text, structured_hints
from fieldnote.textutil import canonical_url, find_entities


@dataclass
class FixtureRound:
    label: str
    as_of: datetime


class FixtureDataset:
    def __init__(self, workspace: str, root: Path | None = None) -> None:
        self.root = (root or fixtures_dir()) / workspace
        manifest_path = self.root / "manifest.json"
        if not manifest_path.exists():
            raise ConfigError(f"no fixture dataset for workspace '{workspace}' at {self.root}")
        self.manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.rounds = [
            FixtureRound(label=r["label"], as_of=dtparser.isoparse(r["as_of"]).replace(tzinfo=None))
            for r in self.manifest["rounds"]
        ]

    def load(self, name: str) -> list[dict[str, Any]]:
        path = self.root / f"{name}.json"
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        return list(data.get("items", data) if isinstance(data, dict) else data)

    def round_index(self, label: str) -> int:
        for i, r in enumerate(self.rounds):
            if r.label == label:
                return i
        raise ConfigError(f"unknown fixture round {label!r}")


def _dt(value: str | None) -> datetime | None:
    return dtparser.isoparse(value).replace(tzinfo=None) if value else None


class FixtureCollector(Collector):
    """Replays one fixture source type for one round."""

    def __init__(
        self, settings: Settings, dataset: FixtureDataset, source_type: str, round_label: str, now: datetime
    ) -> None:
        super().__init__(settings, http=None, now=now)
        self.dataset = dataset
        self.source_type = source_type
        self.round_label = round_label
        self.name = f"fixture:{source_type}"

    def _collect(self, cfg: WorkspaceConfig, since: datetime) -> CollectorResult:
        aliases = cfg.entity_aliases()
        docs: list[CollectedDocument] = []
        if self.source_type == "pages":
            docs = self._pages(cfg)
        else:
            for item in self.dataset.load(self.source_type):
                published = _dt(item.get("published_at") or item.get("created_at"))
                if published is None or not (since < published <= self.now):
                    continue
                docs.append(self._to_doc(item, published, aliases))
        return CollectorResult(name=self.name, documents=docs, status="ok", stats={"items": len(docs), "fixture": True})

    def _to_doc(self, item: dict[str, Any], published: datetime, aliases: dict[str, str]) -> CollectedDocument:
        st = self.source_type
        meta: dict[str, Any] = {"fixture": True}
        if st == "news":
            text, flags = sanitize_text(item.get("content") or item.get("summary", ""))
            meta.update(flags)
            meta["entities"] = find_entities(f"{item['title']}\n{text}", aliases)
            return CollectedDocument(
                source_type="news",
                source_name=item.get("source_name", "Fixture News"),
                url=item["url"],
                canonical_url=canonical_url(item["url"]),
                title=item["title"],
                summary=item.get("summary", ""),
                content=text,
                published_at=published,
                fetched_at=self.now,
                is_primary=bool(item.get("is_primary")),
                metadata=meta,
            )
        if st == "reddit":
            text, flags = sanitize_text(f"{item['title']}\n\n{item.get('text', '')}".strip())
            meta.update(flags)
            meta.update(
                {
                    "score": item.get("score", 0),
                    "num_comments": item.get("num_comments", 0),
                    "subreddit": item.get("subreddit", ""),
                    "entities": find_entities(text, aliases),
                }
            )
            return CollectedDocument(
                source_type="reddit",
                source_name=f"r/{item.get('subreddit', 'fixture')}",
                url=item["permalink"],
                canonical_url=item["permalink"],
                title=item["title"],
                content=text,
                published_at=published,
                fetched_at=self.now,
                metadata=meta,
            )
        if st == "youtube":
            text, flags = sanitize_text(item["text"])
            meta.update(flags)
            meta.update(
                {
                    "video_id": item["video_id"],
                    "video_title": item.get("video_title", ""),
                    "like_count": item.get("like_count", 0),
                    "entities": find_entities(f"{item.get('video_title', '')}\n{text}", aliases),
                }
            )
            url = f"https://video.example/watch?v={item['video_id']}&lc={item['comment_id']}"
            return CollectedDocument(
                source_type="youtube",
                source_name=f"YouTube: {item.get('video_title', '')[:120]}",
                url=url,
                canonical_url=url,
                title=f"Comment on: {item.get('video_title', '')[:200]}",
                content=text,
                published_at=published,
                fetched_at=self.now,
                metadata=meta,
            )
        raise ConfigError(f"unsupported fixture source type {st!r}")

    def _pages(self, cfg: WorkspaceConfig) -> list[CollectedDocument]:
        idx = self.dataset.round_index(self.round_label)
        order = {r.label: i for i, r in enumerate(self.dataset.rounds)}
        latest: dict[str, dict[str, Any]] = {}
        for item in self.dataset.load("pages"):
            ri = order.get(item["round"], 99)
            if ri > idx:
                continue
            key = item["url"]
            if key not in latest or order[latest[key]["round"]] <= ri:
                latest[key] = item
        docs = []
        for url, item in sorted(latest.items()):
            html = item["html"]
            text = extract_main_text(html, url=url)
            text, flags = sanitize_text(text)
            hints = structured_hints(html, text, item.get("selector"))
            docs.append(
                CollectedDocument(
                    source_type="page",
                    source_name=f"{item['competitor']} ({item['kind']})",
                    url=url,
                    canonical_url=url,
                    title=hints["headings"][0] if hints["headings"] else f"{item['competitor']} page",
                    content=text,
                    published_at=self.now,
                    fetched_at=self.now,
                    is_primary=True,
                    metadata={
                        "fixture": True,
                        "competitor": item["competitor"],
                        "page_kind": item["kind"],
                        "page_url": url,
                        "structured": hints,
                        "entities": [item["competitor"]],
                        **flags,
                    },
                )
            )
        return docs


def fixture_collectors(settings: Settings, workspace: str, round_label: str, now: datetime) -> list[FixtureCollector]:
    ds = FixtureDataset(workspace)
    return [FixtureCollector(settings, ds, st, round_label, now) for st in ("news", "pages", "reddit", "youtube")]
