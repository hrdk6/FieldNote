"""Deduplication: content hash, canonical URL, and RapidFuzz title similarity.

Near-duplicate syndicated news collapses to one document; the other outlets are kept in
``metadata['also_seen_at']`` / ``metadata['syndicated_sources']``.
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz

from fieldnote.domain import CollectedDocument
from fieldnote.textutil import canonical_url, content_hash, normalize_ws

TITLE_SIMILARITY = 90  # token_sort_ratio threshold for "same story"


def strip_publisher_suffix(title: str, publisher: str | None = None) -> str:
    """Google News titles end in ' - Publisher'. Strip it so fuzzy matching compares the headline."""
    t = normalize_ws(title)
    if publisher and t.endswith(f" - {publisher}"):
        return t[: -len(publisher) - 3].strip()
    m = re.match(r"^(.*\S)\s+[-|–]\s+[^-|–]{2,60}$", t)
    return m.group(1) if m and len(m.group(1)) > 20 else t


def _normalized_title(title: str) -> str:
    return re.sub(r"[^a-z0-9₹ ]+", " ", strip_publisher_suffix(title).lower()).strip()


def titles_match(a: str, b: str, threshold: int = TITLE_SIMILARITY) -> bool:
    na, nb = _normalized_title(a), _normalized_title(b)
    if not na or not nb or min(len(na), len(nb)) < 15:
        return False
    return fuzz.token_sort_ratio(na, nb) >= threshold


def _richness(d: CollectedDocument) -> tuple[int, int, int]:
    return (int(d.is_primary), len(d.content or ""), len(d.summary or ""))


def dedupe_documents(docs: list[CollectedDocument]) -> list[CollectedDocument]:
    """Collapse exact (hash / canonical URL) and near (fuzzy title) duplicates, keeping the richest copy."""
    kept: list[CollectedDocument] = []
    by_url: dict[str, int] = {}
    by_hash: dict[str, int] = {}
    for d in docs:
        curl = d.canonical_url or canonical_url(d.url)
        d.canonical_url = curl
        h = content_hash(f"{d.title}\n{d.content or d.summary}") if (d.content or d.summary) else ""
        idx = by_url.get(curl)
        if idx is None and h:
            idx = by_hash.get(h)
        if idx is None and d.source_type == "news":
            for i, k in enumerate(kept):
                if k.source_type == "news" and titles_match(k.title, d.title):
                    idx = i
                    break
        if idx is None:
            by_url[curl] = len(kept)
            if h:
                by_hash[h] = len(kept)
            kept.append(d)
            continue
        existing = kept[idx]
        winner, loser = (d, existing) if _richness(d) > _richness(existing) else (existing, d)
        sources = set(winner.metadata.get("syndicated_sources", [])) | set(loser.metadata.get("syndicated_sources", []))
        sources.add(loser.source_name)
        sources.discard(winner.source_name)
        also = set(winner.metadata.get("also_seen_at", [])) | set(loser.metadata.get("also_seen_at", [])) | {loser.url}
        also.discard(winner.url)
        winner.metadata["syndicated_sources"] = sorted(s for s in sources if s)
        winner.metadata["also_seen_at"] = sorted(also)
        kept[idx] = winner
        by_url[winner.canonical_url] = idx
    return kept
