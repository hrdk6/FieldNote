"""Helpers shared by the daily brief, weekly memo, Ask answers and the dashboard."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from sqlalchemy.orm import Session

from fieldnote.agents.tools import document_link
from fieldnote.config import WorkspaceConfig
from fieldnote.db import repo
from fieldnote.textutil import quote_excerpt, replace_entities, split_sentences, truncate_words, word_count

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
TYPE_LABEL = {"fact": "Fact", "estimate": "Estimate", "analysis": "Analysis"}


def jinja_env(autoescape: bool) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        undefined=StrictUndefined,
        # Markdown/plain-text templates are not HTML; the HTML template gets autoescaping.
        autoescape=select_autoescape(["html"]) if autoescape else False,  # noqa: S701
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["pct"] = lambda v, d=0: f"{v * 100:.{d}f}%"
    env.filters["signed"] = lambda v, d=1: f"{v:+.{d}f}"
    return env


@dataclass
class EvidenceItem:
    n: int
    claim_id: int
    type: str
    label: str
    text: str
    quote: str
    source_title: str
    source_name: str
    source_type: str
    url: str
    published: str
    extra_sources: int = 0

    @property
    def show_quote(self) -> bool:
        """Show the source quote only for facts whose text does not already contain it."""
        q = self.quote.rstrip("…").strip()
        return bool(q) and self.type == "fact" and q not in self.text


def evidence_items(s: Session, claim_ids: list[int], start: int = 1, only_verified: bool = True) -> list[EvidenceItem]:
    claims = repo.get_claims(s, claim_ids)
    doc_ids = {int(i) for c in claims.values() for i in (c.source_document_ids or [])}
    docs = repo.get_documents(s, doc_ids)
    out: list[EvidenceItem] = []
    n = start
    for cid in claim_ids:
        c = claims.get(cid)
        if c is None or (only_verified and c.status != "verified"):
            continue
        srcs = [docs[int(i)] for i in (c.source_document_ids or []) if int(i) in docs]
        first = srcs[0] if srcs else None
        # Prefer the document the excerpt was matched in.
        ex_doc = (c.checks or {}).get("excerpt_doc")
        if ex_doc and int(ex_doc) in docs:
            first = docs[int(ex_doc)]
        out.append(
            EvidenceItem(
                n=n,
                claim_id=c.id,
                type=c.type,
                label=TYPE_LABEL.get(c.type, c.type.title()),
                text=c.text,
                quote=quote_excerpt(c.excerpt, 25) if c.excerpt and c.type != "analysis" else "",
                source_title=truncate_words(first.title, 14) if first else "",
                source_name=first.source_name if first else "",
                source_type=first.source_type if first else "",
                url=document_link(first) if first else "",
                published=(first.published_at or first.fetched_at).strftime("%Y-%m-%d") if first else "",
                extra_sources=max(0, len(srcs) - 1),
            )
        )
        n += 1
    return out


class Anonymizer:
    """Neutral labels for entities in prose ("Market Leader", "Challenger A", ...).

    The evidence appendix and source links keep real names; only brief prose is relabelled.
    """

    def __init__(self, cfg: WorkspaceConfig, shares: dict[str, float] | None = None) -> None:
        names = cfg.competitor_names()
        order = sorted(names, key=lambda n: (-shares.get(n, -1.0), names.index(n))) if shares else list(names)
        self.labels: dict[str, str] = {}
        letters = iter("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        for i, name in enumerate(order):
            self.labels[name] = "Market Leader" if (i == 0 and shares) else f"Challenger {next(letters)}"
        self.mapping: dict[str, str] = {}
        for comp in cfg.competitors:
            for alias in comp.all_names():
                self.mapping[alias] = self.labels[comp.name]
        self.enabled = cfg.output.anonymize_entities

    def __call__(self, text: str) -> str:
        if not self.enabled or not text:
            return text
        return replace_entities(text, self.mapping)


# ---------------------------------------------------------------------------------------------
# Telegram (MarkdownV2)
# ---------------------------------------------------------------------------------------------

_MDV2 = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def tg_escape(text: str) -> str:
    return _MDV2.sub(r"\\\1", text or "")


def tg_link(label: str, url: str) -> str:
    safe_url = (url or "").replace("\\", "\\\\").replace(")", "\\)")
    return f"[{tg_escape(label)}]({safe_url})"


def split_telegram(lines: list[str], max_chars: int) -> list[str]:
    """Pack already-escaped lines into messages <= max_chars, never splitting inside an escape."""
    chunks: list[str] = []
    cur = ""
    for line in lines:
        while len(line) > max_chars:
            cut = max_chars
            while cut > 0 and line[cut - 1] == "\\":
                cut -= 1
            head, line = line[:cut], line[cut:]
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(head)
        candidate = f"{cur}\n{line}" if cur else line
        if len(candidate) > max_chars:
            chunks.append(cur)
            cur = line
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


TRIM_ORDER = ("how_to_execute", "what_happened", "why_it_matters", "recommended_action")


def limit_prose(fields: dict[str, str], budget_words: int) -> dict[str, str]:
    """Fit prose fields into a word budget, dropping whole trailing sentences first.

    Trimming only removes text, so the writer's number/entity post-checks still hold.
    """
    out = {k: (v or "").strip() for k, v in fields.items()}

    def total() -> int:
        return sum(word_count(v) for v in out.values())

    for k in TRIM_ORDER:
        if k not in out:
            continue
        sents = split_sentences(out[k])
        while total() > budget_words and len(sents) > 1:
            sents = sents[:-1]
            out[k] = " ".join(sents)
    if total() > budget_words:
        floor = {"how_to_execute": 14, "what_happened": 22, "why_it_matters": 14, "recommended_action": 14}
        for k in TRIM_ORDER:
            over = total() - budget_words
            if over <= 0 or k not in out:
                break
            cap = max(floor.get(k, 12), word_count(out[k]) - over)
            out[k] = truncate_words(out[k], cap)
    return out


def fixture_notice() -> str:
    return "FIXTURE DATA: generated from bundled synthetic fixtures with fictional brands. Not real market information."


def verification_stats(s: Session, workspace: str, run_ids: list[int]) -> dict[str, Any]:
    checked = verified = rejected = unverified = 0
    for rid in run_ids:
        for c in repo.claims_for_run(s, workspace, rid):
            checked += 1
            if c.status == "verified":
                verified += 1
            elif c.status == "rejected":
                rejected += 1
            else:
                unverified += 1
    return {
        "checked": checked,
        "verified": verified,
        "rejected": rejected,
        "unverified": unverified,
        "verified_pct": round(100.0 * verified / checked, 1) if checked else 0.0,
    }
