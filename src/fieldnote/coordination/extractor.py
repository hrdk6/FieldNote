"""Action-item extraction from meeting notes.

The LLM (or the heuristic fallback) proposes items with the due-date *phrase* exactly as written; the
date itself is resolved here, in code, against the note date. Owners are never guessed: a missing,
pronoun or unknown owner becomes ``Unassigned`` with a flag. New items are compared with open items
and a merge is offered instead of creating duplicates.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from rapidfuzz import fuzz

from fieldnote.config import WorkspaceConfig
from fieldnote.costs import BudgetExceededError
from fieldnote.db import repo
from fieldnote.db.engine import Database, utcnow
from fieldnote.db.models import ActionItem
from fieldnote.heuristics import extract_action_items_heuristic, parse_attendees
from fieldnote.llm.base import LLMClient, LLMError
from fieldnote.llm.schemas import ActionItemsOutput
from fieldnote.logging_setup import get_logger
from fieldnote.textutil import normalize_ws

log = get_logger(__name__)

UNASSIGNED = "Unassigned"
MERGE_THRESHOLD = 85
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = (
    {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
    | {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
    | {"sept": 9}
)
NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
PRONOUNS = {
    "he",
    "she",
    "they",
    "we",
    "someone",
    "somebody",
    "team",
    "everyone",
    "anyone",
    "you",
    "i",
    "tbd",
    "all",
    "us",
    "me",
}


# ---------------------------------------------------------------------------------------------
# Relative date resolution (deterministic)
# ---------------------------------------------------------------------------------------------


def _end_of_month(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _friday_of_week(d: date) -> date:
    return d + timedelta(days=(4 - d.weekday()))


def resolve_due(text: str | None, note_date: date) -> date | None:
    """Resolve a due-date phrase relative to ``note_date``.

    Rules (documented in docs/assumptions.md):
    * today / tonight / EOD -> note date; tomorrow -> +1; day after tomorrow -> +2
    * "<weekday>" or "by <weekday>" -> the next occurrence after the note date (same weekday -> +7)
    * "next <weekday>" -> that weekday in the following calendar week (weeks start Monday)
    * "this week" / EOW / "end of (the) week" -> Friday of the note's week (next Friday if it has passed)
    * "next week" -> Friday of the following week
    * "end of (the) month" / EOM -> last day of the month; "next month" -> last day of next month
    * "end of (the) quarter" / EOQ -> last day of the quarter
    * "in N days/weeks" -> note date + N days / weeks
    * explicit dates ("3 Oct", "October 3rd", "2026-10-03"): the next such date on/after the note date
    """
    if not text:
        return None
    t = normalize_ws(text).lower().strip(" .,")
    t = re.sub(r"^(by|before|due|on|until|no later than|latest by)\s+", "", t)
    if t in ("today", "tonight", "eod", "end of day", "end of the day", "asap today"):
        return note_date
    if t == "tomorrow":
        return note_date + timedelta(days=1)
    if t == "day after tomorrow":
        return note_date + timedelta(days=2)
    if t in ("this week", "eow", "end of week", "end of the week"):
        fri = _friday_of_week(note_date)
        return fri if fri >= note_date else fri + timedelta(days=7)
    if t == "next week":
        return _friday_of_week(note_date) + timedelta(days=7)
    if t in ("end of month", "end of the month", "eom", "month end", "this month"):
        return _end_of_month(note_date)
    if t == "next month":
        return _end_of_month(_add_months(note_date.replace(day=1), 1))
    if t in ("end of quarter", "end of the quarter", "eoq", "quarter end"):
        q_end_month = ((note_date.month - 1) // 3 + 1) * 3
        return _end_of_month(date(note_date.year, q_end_month, 1))
    m = re.fullmatch(r"(this|next)\s+(" + "|".join(WEEKDAYS) + r")", t)
    if m:
        wd = WEEKDAYS.index(m.group(2))
        if m.group(1) == "next":
            monday_next = note_date - timedelta(days=note_date.weekday()) + timedelta(days=7)
            return monday_next + timedelta(days=wd)
        delta = (wd - note_date.weekday()) % 7
        return note_date + timedelta(days=delta)
    if t in WEEKDAYS:
        delta = (WEEKDAYS.index(t) - note_date.weekday()) % 7
        return note_date + timedelta(days=delta or 7)
    m = re.fullmatch(r"in\s+(\d+|" + "|".join(NUMBER_WORDS) + r")\s+(day|days|week|weeks)", t)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else NUMBER_WORDS[m.group(1)]
        return note_date + timedelta(days=n * (7 if m.group(2).startswith("week") else 1))
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)\.?(?:\s+(\d{4}))?", t) or None
    day_month: tuple[int, int, int | None] | None = None
    if m and m.group(2) in MONTHS:
        day_month = (int(m.group(1)), MONTHS[m.group(2)], int(m.group(3)) if m.group(3) else None)
    else:
        m2 = re.fullmatch(r"([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", t)
        if m2 and m2.group(1) in MONTHS:
            day_month = (int(m2.group(2)), MONTHS[m2.group(1)], int(m2.group(3)) if m2.group(3) else None)
    if day_month:
        day, month, year = day_month
        try:
            if year:
                return date(year, month, day)
            cand = date(note_date.year, month, day)
            if cand < note_date - timedelta(days=30):
                cand = date(note_date.year + 1, month, day)
            return cand
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------------------------


@dataclass
class ExtractedItem:
    description: str
    owner: str
    due_date: date | None
    due_text: str | None
    priority: str
    confidence: float
    source_sentence: str
    flags: list[str] = field(default_factory=list)


@dataclass
class MergeCandidate:
    index: int
    existing_id: int
    existing_description: str
    score: float


@dataclass
class ExtractionPreview:
    title: str
    note_date: date
    body: str
    items: list[ExtractedItem]
    merges: list[MergeCandidate] = field(default_factory=list)
    via: str = "llm"


_PRONOUN_SUBJECT = re.compile(
    r"(?i)^\s*(?:[-*•]\s*|\d{1,2}[.)]\s+)?(?:[a-z ]+:\s*)?(?:we|someone|somebody|the team|team|everyone|anyone|they|he|she|you|tbd)\b"
)


def _validate_owner(
    owner: str | None, note: str, attendees: list[str], source_sentence: str = ""
) -> tuple[str, list[str]]:
    if not owner or not owner.strip():
        if _PRONOUN_SUBJECT.search(source_sentence) or re.search(r"(?i)\b(tbd|to be decided)\b", source_sentence):
            return UNASSIGNED, ["ambiguous_owner"]
        return UNASSIGNED, ["missing_owner"]
    name = owner.strip().split()[0].strip("@,.:")
    if name.lower() in PRONOUNS or not name[:1].isupper():
        return UNASSIGNED, ["ambiguous_owner"]
    if not re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", note):
        return UNASSIGNED, ["owner_not_in_text"]
    flags = ["owner_not_in_attendees"] if attendees and name not in attendees else []
    return name, flags


def extract_action_items(
    cfg: WorkspaceConfig, llm: LLMClient | None, note: str, note_date: date
) -> tuple[list[ExtractedItem], str]:
    attendees = parse_attendees(note)
    drafts: list[dict[str, object]] = []
    via = "heuristic"
    if llm is not None:
        try:
            out = llm.structured(
                task="action_items",
                system=(
                    "Extract action items from internal meeting notes. For each: a short imperative description, the owner's "
                    "name exactly as written (null if no specific person is named; never guess from pronouns like 'we'), "
                    "the due-date phrase exactly as written (e.g. 'by Friday', 'next week', '3 Oct'; null if none), "
                    "priority (high for urgent/ASAP/blocker, low for nice-to-have), confidence 0-1, and the source sentence "
                    "copied verbatim. Do not include decisions or FYIs that have no follow-up task."
                ),
                prompt=f"Note date: {note_date.isoformat()}\n\n{note}",
                schema=ActionItemsOutput,
                tier="fast",
                payload={"note": note, "note_date": note_date.isoformat()},
            )
            drafts = [d.model_dump() for d in out.items]
            via = "llm"
        except (LLMError, BudgetExceededError) as exc:
            log.info("action-item extraction fell back to heuristics: %s", exc)
            drafts = extract_action_items_heuristic(note)  # type: ignore[assignment]
    else:
        drafts = extract_action_items_heuristic(note)  # type: ignore[assignment]
    items: list[ExtractedItem] = []
    norm_note = normalize_ws(note).lower()
    for d in drafts:
        owner, flags = _validate_owner(
            d.get("owner") if isinstance(d.get("owner"), str) else None,  # type: ignore[arg-type]
            note,
            attendees,
            str(d.get("source_sentence") or ""),
        )
        extra_flags: Any = d.get("flags") or []
        flags = sorted(set(flags) | set(extra_flags))
        if owner == UNASSIGNED:
            flags = [f for f in flags if f != "owner_not_in_attendees"]
        due_text = d.get("due_text") if isinstance(d.get("due_text"), str) else None
        due = resolve_due(due_text, note_date)  # type: ignore[arg-type]
        if due_text and due is None:
            flags.append("unresolved_due_date")
        src = str(d.get("source_sentence") or "")
        if src and fuzz.partial_ratio(normalize_ws(src).lower(), norm_note) < 85:
            flags.append("source_not_verbatim")
        items.append(
            ExtractedItem(
                description=str(d.get("description") or "").strip()[:400],
                owner=owner,
                due_date=due,
                due_text=due_text,  # type: ignore[arg-type]
                priority=str(d.get("priority") or "medium"),
                confidence=float(d.get("confidence") or 0.5),  # type: ignore[arg-type]
                source_sentence=src[:600],
                flags=sorted(set(flags)),
            )
        )
    return [i for i in items if i.description], via


def find_merge_candidates(items: list[ExtractedItem], open_items: list[ActionItem]) -> list[MergeCandidate]:
    out = []
    for idx, it in enumerate(items):
        best: MergeCandidate | None = None
        for ex in open_items:
            score = fuzz.token_set_ratio(it.description.lower(), ex.description.lower())
            owner_ok = it.owner == ex.owner or UNASSIGNED in (it.owner, ex.owner)
            if score >= MERGE_THRESHOLD and owner_ok and (best is None or score > best.score):
                best = MergeCandidate(
                    index=idx, existing_id=ex.id, existing_description=ex.description, score=float(score)
                )
        if best:
            out.append(best)
    return out


def preview_note(
    db: Database, cfg: WorkspaceConfig, llm: LLMClient | None, body: str, note_date: date, title: str = ""
) -> ExtractionPreview:
    items, via = extract_action_items(cfg, llm, body, note_date)
    with db.session() as s:
        open_items = repo.open_action_items(s, cfg.workspace)
        merges = find_merge_candidates(items, open_items)
    if llm is not None:
        llm.cache.flush()
    return ExtractionPreview(
        title=title or f"Meeting notes {note_date.isoformat()}",
        note_date=note_date,
        body=body,
        items=items,
        merges=merges,
        via=via,
    )


def commit_preview(
    db: Database,
    cfg: WorkspaceConfig,
    preview: ExtractionPreview,
    *,
    accept_merges: bool = True,
    selected: list[int] | None = None,
) -> dict[str, Any]:
    """Store the note and its items. Items matched to an open item are merged (history kept) if accepted."""
    merge_by_index = {m.index: m for m in preview.merges} if accept_merges else {}
    created: list[int] = []
    merged: list[int] = []
    with db.session() as s:
        note = repo.add_note(s, cfg.workspace, preview.title, preview.body, preview.note_date)
        for idx, it in enumerate(preview.items):
            if selected is not None and idx not in selected:
                continue
            m = merge_by_index.get(idx)
            if m is not None:
                ex = repo.get_action_item(s, cfg.workspace, m.existing_id)
                if ex is not None:
                    history = list(ex.history or [])
                    history.append(
                        {
                            "merged_at": utcnow().isoformat(timespec="seconds"),
                            "note_id": note.id,
                            "source_sentence": it.source_sentence,
                            "due_date": it.due_date.isoformat() if it.due_date else None,
                        }
                    )
                    ex.history = history
                    if it.due_date and (ex.due_date is None or it.due_date < ex.due_date):
                        ex.due_date = it.due_date
                    if ex.owner == UNASSIGNED and it.owner != UNASSIGNED:
                        ex.owner = it.owner
                    merged.append(ex.id)
                    continue
            row = ActionItem(
                workspace=cfg.workspace,
                note_id=note.id,
                description=it.description,
                owner=it.owner,
                due_date=it.due_date,
                priority=it.priority if it.priority in ("low", "medium", "high") else "medium",
                status="open",
                confidence=it.confidence,
                source_sentence=it.source_sentence,
                flags=it.flags,
            )
            s.add(row)
            s.flush()
            created.append(row.id)
        note_id = note.id
    return {"note_id": note_id, "created": created, "merged": merged}
