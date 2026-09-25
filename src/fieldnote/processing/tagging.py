"""Entity tagging and customer-voice tagging.

* Entity tagging matches competitor names/aliases deterministically; short or dictionary-like aliases
  are "ambiguous" and those mentions are resolved by the fast LLM. Unknown capitalised names are kept
  in ``metadata['unknown_entities']``.
* Voice tagging batches the fast LLM over new social/forum documents: entities, aspect (from the
  workspace taxonomy or ``other``), sentiment -1..1, a short supporting quote, and language.
  Original text is kept untouched.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from sqlalchemy.orm import Session

from fieldnote.config import WorkspaceConfig
from fieldnote.db import repo
from fieldnote.db.models import Document, VoiceTag
from fieldnote.heuristics import best_quote, detect_aspect, detect_language, sentiment_score
from fieldnote.llm.base import LLMClient, LLMError
from fieldnote.llm.schemas import EntityResolutionOutput, VoiceTagOutput
from fieldnote.logging_setup import get_logger
from fieldnote.textutil import UNTRUSTED_NOTICE, find_entities, proper_nouns, truncate_words, wrap_untrusted

log = get_logger(__name__)

VOICE_BATCH = 10
# Generic English words that brands often borrow; a match on one of these alone is not trusted.
_COMMON_WORDS = {
    "simple",
    "river",
    "bounce",
    "hero",
    "pure",
    "ultra",
    "one",
    "real",
    "apple",
    "nothing",
    "honor",
    "pixel",
    "note",
    "lava",
    "spark",
    "volt",
    "zip",
    "ace",
    "mint",
    "live",
    "go",
    "air",
    "plus",
    "max",
    "pro",
    "prime",
    "neo",
    "edge",
    "nova",
    "orbit",
    "swift",
    "rapid",
    "bolt",
    "flash",
    "echo",
    "pulse",
    "wave",
    "zen",
}


def ambiguous_aliases(cfg: WorkspaceConfig) -> set[str]:
    """Aliases that are short or are ordinary words: mentions need confirmation by the fast model."""
    out = set()
    for alias in cfg.entity_aliases():
        low = alias.lower()
        if len(alias) <= 3 or low in _COMMON_WORDS or alias.islower():
            out.add(alias)
    return out


def tag_document_entities(docs: list[Document], cfg: WorkspaceConfig, llm: LLMClient | None) -> dict[str, int]:
    aliases = cfg.entity_aliases()
    amb = ambiguous_aliases(cfg)
    unambiguous = {a: c for a, c in aliases.items() if a not in amb}
    needs_llm: list[Document] = []
    known_lower = {a.lower() for a in aliases}
    for d in docs:
        text = f"{d.title}\n{d.content}"
        firm = find_entities(text, unambiguous)
        loose = find_entities(text, aliases)
        ents = list(dict.fromkeys([*d.entities, *firm]))
        if set(loose) - set(ents):
            needs_llm.append(d)
        d.entities = ents
        unknown = [
            n for n, _ in Counter(p for p in proper_nouns(d.title) if p.lower() not in known_lower).most_common(5)
        ]
        if unknown:
            meta = dict(d.meta or {})
            meta["unknown_entities"] = unknown
            d.meta = meta
    resolved = 0
    if needs_llm and llm is not None:
        for i in range(0, len(needs_llm), VOICE_BATCH):
            batch = needs_llm[i : i + VOICE_BATCH]
            try:
                out = llm.structured(
                    task="entity_resolution",
                    system=(
                        "Decide which of the known competitors each document actually refers to. Short or common-word "
                        f"brand names can be false matches. Known competitors: {', '.join(cfg.competitor_names())}. "
                        f"{UNTRUSTED_NOTICE}"
                    ),
                    prompt="\n\n".join(
                        f"[doc {d.id}] {wrap_untrusted(truncate_words(d.title + ' ' + d.content, 250))}" for d in batch
                    ),
                    schema=EntityResolutionOutput,
                    tier="fast",
                    payload={
                        "documents": [{"id": d.id, "text": f"{d.title}\n{d.content}"} for d in batch],
                        "aliases": aliases,
                    },
                )
            except LLMError as exc:
                log.info("entity resolution skipped: %s", exc)
                break
            valid = set(cfg.competitor_names())
            by_id = {d.id: d for d in batch}
            for item in out.items:
                doc = by_id.get(item.document_id)
                if doc is None:
                    continue
                add = [e for e in item.entities if e in valid and e not in doc.entities]
                if add:
                    doc.entities = [*doc.entities, *add]
                    resolved += len(add)
    return {"documents": len(docs), "sent_to_llm": len(needs_llm), "resolved_by_llm": resolved}


def _heuristic_voice(d: Document, cfg: WorkspaceConfig) -> dict[str, object]:
    text = f"{d.title}\n{d.content}"
    return {
        "entities": find_entities(text, cfg.entity_aliases()),
        "aspect": detect_aspect(text, cfg.aspect_term_map()),
        "sentiment": sentiment_score(d.content or d.title),
        "quote": best_quote(d.content or d.title),
        "language": detect_language(text, cfg.language_hints),
    }


def _valid_quote(quote: str, text: str) -> bool:
    q = re.sub(r"\W+", " ", quote.lower()).strip()
    t = re.sub(r"\W+", " ", text.lower())
    return bool(q) and q.rstrip(" …") in t


def tag_voice(
    s: Session, cfg: WorkspaceConfig, run_id: int, llm: LLMClient | None, until: object = None
) -> dict[str, int]:
    docs = repo.untagged_social_documents(s, cfg.workspace, until=until)  # type: ignore[arg-type]
    aspects = set(cfg.aspects)
    names = set(cfg.competitor_names())
    stats = {"documents": len(docs), "llm_tagged": 0, "heuristic_tagged": 0}
    for i in range(0, len(docs), VOICE_BATCH):
        batch = docs[i : i + VOICE_BATCH]
        results: dict[int, dict[str, object]] = {}
        if llm is not None:
            try:
                out = llm.structured(
                    task="voice_tagging",
                    system=(
                        f"You tag customer posts about {cfg.sector} in {cfg.region}. Posts may be English or Hinglish. "
                        f"Aspects: {', '.join(cfg.aspects)} (use 'other' if none fit). Competitors: "
                        f"{', '.join(cfg.competitor_names())}. Sentiment is -1 (very negative) to 1 (very positive) toward "
                        f"the product/brand. The quote must be copied verbatim from the post, max 25 words. {UNTRUSTED_NOTICE}"
                    ),
                    prompt="\n\n".join(
                        f"[doc {d.id}] {wrap_untrusted(truncate_words(d.title + chr(10) + d.content, 300))}"
                        for d in batch
                    ),
                    schema=VoiceTagOutput,
                    tier="fast",
                    payload={
                        "documents": [{"id": d.id, "text": f"{d.title}\n{d.content}"} for d in batch],
                        "aspect_terms": cfg.aspect_term_map(),
                        "aliases": cfg.entity_aliases(),
                        "language_hints": cfg.language_hints,
                    },
                )
                for it in out.items:
                    results[it.document_id] = it.model_dump()
            except LLMError as exc:
                log.info("voice tagging fell back to heuristics: %s", exc)
        for d in batch:
            r = results.get(d.id)
            if r is None:
                r = _heuristic_voice(d, cfg)
                stats["heuristic_tagged"] += 1
            else:
                stats["llm_tagged"] += 1
            aspect = str(r.get("aspect") or "other")
            if aspect not in aspects:
                aspect = "other"
            raw_ents: Any = r.get("entities") or []
            ents = [e for e in raw_ents if e in names] or list(d.entities or [])
            quote = str(r.get("quote") or "")
            if not _valid_quote(quote, f"{d.title} {d.content}"):
                quote = best_quote(d.content or d.title)
            sentiment = max(-1.0, min(1.0, float(r.get("sentiment", 0.0) or 0.0)))  # type: ignore[arg-type]
            s.add(
                VoiceTag(
                    workspace=cfg.workspace,
                    document_id=d.id,
                    run_id=run_id,
                    entities=ents,
                    aspect=aspect,
                    sentiment=sentiment,
                    stance_quote=truncate_words(quote, 25),
                    language=str(r.get("language") or "en"),
                )
            )
            d.language = str(r.get("language") or d.language)
        s.flush()
    return stats
