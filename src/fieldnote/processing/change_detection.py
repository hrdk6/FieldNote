"""Competitor page change detection.

1. Normalise page text: collapse whitespace, drop boilerplate lines, mask volatile tokens
   (timestamps, counters, cookie banners) and apply workspace ``ignore_patterns``.
2. ``difflib`` over line segments; drop trivial diffs using configurable thresholds.
3. Classify with deterministic rules first (price regex delta, new headings/launch words, policy,
   dealer, feature vocab) and the fast LLM second for anything left as ``other``.
4. Score significance 0-1 and emit ``change_events`` with before/after and a neutral summary.

Every change also produces a *change record* document (source_type=page, primary) whose content
holds the captured before/after text. Claims about the change cite that record, so excerpts and
numbers can be verified mechanically.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from fieldnote.config import ChangeDetectionConfig, WorkspaceConfig
from fieldnote.db import repo
from fieldnote.db.models import ChangeEvent, PageSnapshot
from fieldnote.domain import CollectedDocument
from fieldnote.heuristics import classify_change_rules
from fieldnote.llm.base import LLMClient, LLMError
from fieldnote.llm.schemas import ChangeClassificationOutput
from fieldnote.logging_setup import get_logger
from fieldnote.textutil import (
    UNTRUSTED_NOTICE,
    content_hash,
    extract_numbers,
    normalize_ws,
    pct_change,
    truncate_words,
    wrap_untrusted,
)

log = get_logger(__name__)

_BOILERPLATE = re.compile(
    r"(?i)(cookie|we use cookies|accept all|privacy policy|terms of use|all rights reserved|©|copyright|"
    r"subscribe to our newsletter|follow us|sign in|log in|skip to content|back to top)"
)
_VOLATILE = [
    (re.compile(r"(?i)\b(?:last\s+)?updated\s*(?:on|at)?\s*:?\s*[^\n]{0,40}"), "<updated>"),
    (re.compile(r"(?i)\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm|ist|utc|gmt)?\b"), "<time>"),
    (re.compile(r"(?i)\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s+\d{4}\b"), "<date>"),
    (re.compile(r"(?i)\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\b"), "<date>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?\b"), "<date>"),
    (
        re.compile(
            r"(?i)\b[\d,]+\s+(?:views?|people (?:viewing|watching)|visitors?|bookings? today|online now|ratings?|reviews?)\b"
        ),
        "<count>",
    ),
    (re.compile(r"(?i)\b\d+\s+(?:seconds?|minutes?|hours?|days?)\s+ago\b"), "<ago>"),
    (re.compile(r"(?i)\bsession\s*id[:=]?\s*\w+"), "<session>"),
]

BASE_SIGNIFICANCE = {"price": 0.6, "launch": 0.8, "policy": 0.6, "dealer": 0.5, "feature": 0.5, "other": 0.25}


def normalize_segments(text: str, cfg: ChangeDetectionConfig) -> list[str]:
    ignore = [re.compile(p) for p in cfg.ignore_patterns]
    out: list[str] = []
    for raw in (text or "").splitlines():
        line = normalize_ws(raw)
        if not line or _BOILERPLATE.search(line):
            continue
        if any(p.search(line) for p in ignore):
            continue
        for pat, repl in _VOLATILE:
            line = pat.sub(repl, line)
        line = normalize_ws(line)
        if not line or re.fullmatch(r"(?:<\w+>\s*)+", line):
            continue
        out.append(line)
    return out


@dataclass
class DiffBlock:
    before: list[str]
    after: list[str]
    op: str

    @property
    def before_text(self) -> str:
        return "\n".join(self.before)

    @property
    def after_text(self) -> str:
        return "\n".join(self.after)

    def changed_chars(self) -> int:
        sm = difflib.SequenceMatcher(a=self.before_text, b=self.after_text, autojunk=False)
        same = sum(b.size for b in sm.get_matching_blocks())
        return max(len(self.before_text), len(self.after_text)) - same


def diff_segments(before: list[str], after: list[str]) -> list[DiffBlock]:
    sm = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    blocks = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        blocks.append(DiffBlock(before=before[i1:i2], after=after[j1:j2], op=op))
    return blocks


def is_significant(block: DiffBlock, total_chars: int, cfg: ChangeDetectionConfig) -> bool:
    changed = block.changed_chars()
    # Any change in a currency amount always counts, however small.
    b_prices = sorted(n.value for n in extract_numbers(block.before_text) if n.has_currency)
    a_prices = sorted(n.value for n in extract_numbers(block.after_text) if n.has_currency)
    if b_prices != a_prices and (b_prices or a_prices):
        return True
    # Changed figures (warranty years, outlet counts...) matter even when only a few characters differ.
    # Volatile numbers (timestamps, counters) were already masked by normalize_segments.
    b_nums = sorted(n.value for n in extract_numbers(block.before_text))
    a_nums = sorted(n.value for n in extract_numbers(block.after_text))
    if b_nums != a_nums and b_nums and a_nums:
        return True
    ratio = changed / max(1, total_chars)
    return changed >= cfg.min_changed_chars and ratio >= cfg.min_change_ratio


@dataclass
class DetectedChange:
    competitor: str
    url: str
    page_kind: str
    kind: str
    confidence: float
    before: str
    after: str
    significance: float
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


def _price_pairs(before: str, after: str) -> list[tuple[str, str, str, float | None]]:
    """(label, before_raw, after_raw, pct) for currency amounts that changed, matched by line position."""
    pairs = []
    b_lines = before.splitlines() or [before]
    a_lines = after.splitlines() or [after]
    for bl, al in zip(b_lines, a_lines, strict=False):
        bn = [n for n in extract_numbers(bl) if n.has_currency]
        an = [n for n in extract_numbers(al) if n.has_currency]
        for b, a in zip(bn, an, strict=False):
            if abs(b.value - a.value) > 1e-9:
                label = normalize_ws(al[: a.start]).strip(" :-–|") or normalize_ws(bl[: b.start]).strip(" :-–|")
                pairs.append((label[-60:], b.raw, a.raw, pct_change(b.value, a.value)))
    return pairs


def _summarize(competitor: str, page_kind: str, kind: str, before: str, after: str) -> tuple[str, dict[str, Any]]:
    where = f"{competitor} {page_kind} page"
    details: dict[str, Any] = {}
    if kind == "price":
        pairs = _price_pairs(before, after)
        if pairs:
            label, b, a, pct = pairs[0]
            details["price_change_pct"] = round(pct, 1) if pct is not None else None
            details["price_pairs"] = [{"label": p[0], "before": p[1], "after": p[2], "pct": p[3]} for p in pairs]
            pct_txt = f" ({pct:+.1f}%)" if pct is not None else ""
            what = f" for '{label}'" if label else ""
            return f"{where}: listed price{what} changed from {b} to {a}{pct_txt}.", details
        bn = [n for n in extract_numbers(before) if n.has_currency]
        an = [n for n in extract_numbers(after) if n.has_currency]
        if bn and an:
            pct = pct_change(bn[0].value, an[0].value)
            details["price_change_pct"] = round(pct, 1) if pct is not None else None
            return f"{where}: listed price changed from {bn[0].raw} to {an[0].raw}.", details
        if an:
            return f"{where}: new price listed: {an[0].raw}.", details
    after_snip = truncate_words(after, 18, "…") if after else ""
    before_snip = truncate_words(before, 12, "…") if before else ""
    b_nums = [n.value for n in extract_numbers(before) if not n.has_currency]
    a_nums = [n.value for n in extract_numbers(after) if not n.has_currency]
    trend = ""
    if b_nums and a_nums and len(b_nums) == len(a_nums) and b_nums != a_nums:
        if all(a <= b for a, b in zip(a_nums, b_nums, strict=True)):
            details["numeric_direction"] = "down"
            trend = " (figures reduced)"
        elif all(a >= b for a, b in zip(a_nums, b_nums, strict=True)):
            details["numeric_direction"] = "up"
            trend = " (figures increased)"
    if kind == "launch":
        return f'{where}: new content added: "{after_snip}".', details
    if kind == "dealer":
        b_counts = [n.value for n in extract_numbers(before) if not n.has_currency]
        a_counts = [n.value for n in extract_numbers(after) if not n.has_currency]
        if b_counts and a_counts and b_counts[0] != a_counts[0]:
            details["count_change"] = [b_counts[0], a_counts[0]]
        return f'{where}: retail or dealer information changed to "{after_snip}"{trend}.', details
    if not after:
        return f'{where}: content removed: "{before_snip}".', details
    if not before:
        return f'{where}: {kind} content added: "{after_snip}".', details
    return f'{where}: {kind} wording changed to "{after_snip}" (was "{before_snip}"){trend}.', details


def _significance(kind: str, block: DiffBlock, details: dict[str, Any]) -> float:
    base = BASE_SIGNIFICANCE.get(kind, 0.25)
    if kind == "price" and details.get("price_change_pct") is not None:
        return round(min(1.0, base + abs(details["price_change_pct"]) / 25.0), 3)
    size = min(1.0, block.changed_chars() / 300.0)
    return round(min(1.0, base * (0.6 + 0.4 * size)), 3)


def detect_changes(
    competitor: str,
    url: str,
    page_kind: str,
    before_text: str,
    after_text: str,
    cfg: ChangeDetectionConfig,
    llm: LLMClient | None = None,
) -> list[DetectedChange]:
    before = normalize_segments(before_text, cfg)
    after = normalize_segments(after_text, cfg)
    if before == after:
        return []
    total = max(len("\n".join(before)), len("\n".join(after)))
    blocks = [b for b in diff_segments(before, after) if is_significant(b, total, cfg)]
    if not blocks:
        return []
    classified: list[tuple[DiffBlock, str, float]] = []
    for b in blocks:
        kind, conf = classify_change_rules(b.before_text, b.after_text)
        classified.append((b, kind, conf))
    # Fast LLM second: only for blocks the rules could not place.
    unresolved = [i for i, (_, k, _) in enumerate(classified) if k == "other"]
    if unresolved and llm is not None:
        try:
            out = llm.structured(
                task="change_classification",
                system=(
                    "You classify changes on competitor web pages as price, feature, launch, policy, dealer or "
                    f"other. {UNTRUSTED_NOTICE}"
                ),
                prompt="\n\n".join(
                    f"[{i}] BEFORE: {wrap_untrusted(classified[i][0].before_text[:800])}\n"
                    f"AFTER: {wrap_untrusted(classified[i][0].after_text[:800])}"
                    for i in unresolved
                ),
                schema=ChangeClassificationOutput,
                tier="fast",
                payload={
                    "changes": [
                        {"index": i, "before": classified[i][0].before_text, "after": classified[i][0].after_text}
                        for i in unresolved
                    ]
                },
            )
            for item in out.items:
                if 0 <= item.index < len(classified) and item.index in unresolved:
                    b, _, _ = classified[item.index]
                    classified[item.index] = (b, item.kind, item.confidence)
        except LLMError as exc:
            log.info("change classification LLM step skipped: %s", exc)
    changes = []
    for b, kind, conf in classified:
        summary, details = _summarize(competitor, page_kind, kind, b.before_text, b.after_text)
        details["op"] = b.op
        changes.append(
            DetectedChange(
                competitor=competitor,
                url=url,
                page_kind=page_kind,
                kind=kind,
                confidence=conf,
                before=b.before_text,
                after=b.after_text,
                significance=_significance(kind, b, details),
                summary=summary,
                details=details,
            )
        )
    return changes


def change_record_text(change: DetectedChange, before_date: datetime, after_date: datetime) -> str:
    return "\n".join(
        [
            f"Page: {change.competitor} {change.page_kind} page ({change.url})",
            f"Change captured by FieldNote on {after_date:%Y-%m-%d} (previous capture {before_date:%Y-%m-%d}).",
            f"Before ({before_date:%Y-%m-%d}): {change.before or '(not present)'}",
            f"After ({after_date:%Y-%m-%d}): {change.after or '(removed)'}",
            f"Summary: {change.summary}",
        ]
    )


def process_page_documents(
    s: Session,
    cfg: WorkspaceConfig,
    run_id: int,
    page_docs: list[CollectedDocument],
    now: datetime,
    llm: LLMClient | None = None,
) -> dict[str, int]:
    """Snapshot each fetched page, diff against the previous snapshot, and store change events."""
    stats = {"pages": 0, "snapshots": 0, "changes": 0, "unchanged": 0, "first_seen": 0}
    for doc in page_docs:
        stats["pages"] += 1
        url = doc.metadata.get("page_url", doc.url)
        competitor = doc.metadata.get("competitor", doc.source_name)
        kind = doc.metadata.get("page_kind", "product")
        norm = "\n".join(normalize_segments(doc.content, cfg.change_detection))
        h = content_hash(norm)
        prev = repo.last_snapshot(s, cfg.workspace, url, before=now)
        if prev is not None and prev.hash == h:
            stats["unchanged"] += 1
            continue
        snap_doc = CollectedDocument(
            source_type="page",
            source_name=doc.source_name,
            url=f"{url}#fn-snapshot-{h[:10]}",
            canonical_url=url,
            title=doc.title,
            content=doc.content,
            published_at=now,
            fetched_at=now,
            is_primary=True,
            metadata={**doc.metadata, "kind": "page_snapshot", "link": url},
        )
        stored, _ = repo.upsert_document(s, cfg.workspace, snap_doc, run_id)
        snap = PageSnapshot(
            workspace=cfg.workspace,
            run_id=run_id,
            competitor=competitor,
            url=url,
            kind=kind,
            fetched_at=now,
            text=doc.content,
            structured=doc.metadata.get("structured", {}),
            hash=h,
            document_id=stored.id,
        )
        s.add(snap)
        s.flush()
        stats["snapshots"] += 1
        if prev is None:
            stats["first_seen"] += 1
            continue
        for ch in detect_changes(competitor, url, kind, prev.text, doc.content, cfg.change_detection, llm):
            record = CollectedDocument(
                source_type="page",
                source_name=f"{competitor} ({kind}) change record",
                url=f"{url}#fn-change-{now:%Y%m%d}-{content_hash(ch.summary)[:8]}",
                canonical_url=url,
                title=f"Change on {competitor} {kind} page",
                content=change_record_text(ch, prev.fetched_at, now),
                published_at=now,
                fetched_at=now,
                is_primary=True,
                metadata={
                    "kind": "page_change",
                    "competitor": competitor,
                    "entities": [competitor],
                    "link": url,
                    "fixture": bool(doc.metadata.get("fixture")),
                },
            )
            rec_doc, _ = repo.upsert_document(s, cfg.workspace, record, run_id)
            s.add(
                ChangeEvent(
                    workspace=cfg.workspace,
                    run_id=run_id,
                    competitor=competitor,
                    url=url,
                    kind=ch.kind,
                    before=ch.before,
                    after=ch.after,
                    significance=ch.significance,
                    detected_at=now,
                    snapshot_id=snap.id,
                    prev_snapshot_id=prev.id,
                    document_id=rec_doc.id,
                    summary=ch.summary,
                    details={**ch.details, "confidence": ch.confidence, "page_kind": kind},
                )
            )
            stats["changes"] += 1
    return stats
