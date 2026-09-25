"""Claimed vs owner-reported values for configured metrics (generic: range in km, battery life in hours...).

* Claimed values come from competitor pages (regex by default, or the fast LLM) with the source id.
* Reported values come from customer posts (LLM extraction of number, unit and conditions such as
  city / riding style / season; regex fallback).
* Per entity we report median, IQR and n; n < ``min_n`` is flagged ``low_confidence`` and outputs
  must never state that comparison as fact. Excerpts are stored for verification.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from fieldnote.config import ClaimVsReportedConfig, WorkspaceConfig
from fieldnote.db import repo
from fieldnote.db.models import ClaimVsReported, Document, PageSnapshot
from fieldnote.domain import SOCIAL_SOURCE_TYPES
from fieldnote.heuristics import extract_claimed_value, extract_conditions, extract_reported_value
from fieldnote.llm.base import LLMClient, LLMError
from fieldnote.llm.schemas import ClaimedValuesOutput, ReportedValuesOutput
from fieldnote.logging_setup import get_logger
from fieldnote.textutil import UNTRUSTED_NOTICE, find_entities, split_sentences, truncate_words, wrap_untrusted

log = get_logger(__name__)

REPORT_WINDOW_DAYS = 90
BATCH = 10


def _latest_page_docs(s: Session, workspace: str, now: datetime) -> list[tuple[str, Document]]:
    """(competitor, latest snapshot document) per tracked page URL."""
    snaps = list(
        s.scalars(
            select(PageSnapshot)
            .where(PageSnapshot.workspace == workspace, PageSnapshot.fetched_at <= now)
            .order_by(PageSnapshot.url, PageSnapshot.fetched_at.desc(), PageSnapshot.id.desc())
        )
    )
    seen: set[str] = set()
    out = []
    docs = repo.get_documents(s, [sn.document_id for sn in snaps if sn.document_id])
    for sn in snaps:
        if sn.url in seen or sn.document_id not in docs:
            continue
        seen.add(sn.url)
        out.append((sn.competitor, docs[sn.document_id]))
    return out


def _verbatim_excerpt(excerpt: str, text: str, value: float) -> str:
    if excerpt and re.sub(r"\s+", " ", excerpt.strip().lower()) in re.sub(r"\s+", " ", text.lower()):
        return truncate_words(excerpt.strip(), 40, "")
    num = f"{value:g}"
    for sent in split_sentences(text):
        if num in sent.replace(",", ""):
            return truncate_words(sent, 40, "")
    return truncate_words(text, 30, "")


def _claimed(
    spec: ClaimVsReportedConfig, pages: list[tuple[str, Document]], llm: LLMClient | None
) -> dict[str, tuple[float, int, str]]:
    units, keywords = spec.all_units(), spec.all_keywords()
    out: dict[str, tuple[float, int, str]] = {}
    if spec.claimed_extract == "llm" and llm is not None and pages:
        try:
            res = llm.structured(
                task="claimed_values",
                system=f"Extract the officially claimed {spec.metric} (in {spec.unit}) for each product page. {UNTRUSTED_NOTICE}",
                prompt="\n\n".join(f"[doc {d.id}, brand {c}] {wrap_untrusted(d.content[:3000])}" for c, d in pages),
                schema=ClaimedValuesOutput,
                tier="fast",
                payload={
                    "documents": [{"id": d.id, "text": d.content, "entity": c} for c, d in pages],
                    "units": units,
                    "keywords": keywords,
                    "unit": spec.unit,
                },
            )
            by_id = {d.id: (c, d) for c, d in pages}
            for it in res.items:
                if it.document_id in by_id:
                    comp, doc = by_id[it.document_id]
                    if comp not in out or it.value > out[comp][0]:
                        out[comp] = (it.value, doc.id, _verbatim_excerpt(it.excerpt, doc.content, it.value))
            if out:
                return out
        except LLMError as exc:
            log.info("claimed-value LLM extraction fell back to regex: %s", exc)
    for comp, doc in pages:
        v = extract_claimed_value(doc.content, units, keywords)
        if v and (comp not in out or v.value > out[comp][0]):
            out[comp] = (v.value, doc.id, truncate_words(v.sentence, 40, ""))
    return out


def _reported(spec: ClaimVsReportedConfig, docs: list[Document], llm: LLMClient | None) -> dict[int, dict[str, Any]]:
    units, keywords = spec.all_units(), spec.all_keywords()
    unit_re = re.compile(rf"(?i)\d\s?({'|'.join(re.escape(u) for u in units)})(?![a-z])")
    candidates = [d for d in docs if unit_re.search(d.content or "")]
    found: dict[int, dict[str, Any]] = {}
    if spec.reported_extract == "llm" and llm is not None:
        for i in range(0, len(candidates), BATCH):
            batch = candidates[i : i + BATCH]
            try:
                res = llm.structured(
                    task="reported_values",
                    system=(
                        f"From each customer post, extract the owner's own first-hand {spec.metric} figure in {spec.unit} "
                        "(not a quote of the official claim). Include conditions if stated (city, riding_style, season). "
                        f"Return value null if there is no first-hand figure. {UNTRUSTED_NOTICE}"
                    ),
                    prompt="\n\n".join(f"[doc {d.id}] {wrap_untrusted(truncate_words(d.content, 250))}" for d in batch),
                    schema=ReportedValuesOutput,
                    tier="fast",
                    payload={
                        "documents": [{"id": d.id, "text": d.content} for d in batch],
                        "units": units,
                        "keywords": keywords,
                        "unit": spec.unit,
                    },
                )
                by_id = {d.id: d for d in batch}
                for it in res.items:
                    d = by_id.get(it.document_id)
                    if d is None or it.value is None or not it.first_hand:
                        continue
                    found[d.id] = {
                        "value": float(it.value),
                        "conditions": dict(it.conditions),
                        "excerpt": _verbatim_excerpt(it.excerpt, d.content, float(it.value)),
                    }
            except LLMError as exc:
                log.info("reported-value LLM extraction fell back to regex for a batch: %s", exc)
                for d in batch:
                    v = extract_reported_value(d.content, units, keywords)
                    if v:
                        found[d.id] = {
                            "value": v.value,
                            "conditions": extract_conditions(v.sentence),
                            "excerpt": truncate_words(v.sentence, 40, ""),
                        }
    else:
        for d in candidates:
            v = extract_reported_value(d.content, units, keywords)
            if v:
                found[d.id] = {
                    "value": v.value,
                    "conditions": extract_conditions(v.sentence),
                    "excerpt": truncate_words(v.sentence, 40, ""),
                }
    return found


def iqr_stats(values: list[float]) -> tuple[float, float, float]:
    arr = np.asarray(sorted(values), dtype=float)
    q1, med, q3 = np.percentile(arr, [25, 50, 75])
    return float(med), float(q1), float(q3)


def compute_claim_vs_reported(
    s: Session, cfg: WorkspaceConfig, run_id: int, now: datetime, llm: LLMClient | None
) -> dict[str, Any]:
    if not cfg.claim_vs_reported:
        return {"status": "skipped", "reason": "no claim_vs_reported metrics configured", "rows": 0}
    pages = _latest_page_docs(s, cfg.workspace, now)
    posts = repo.documents_between(s, cfg.workspace, now - timedelta(days=REPORT_WINDOW_DAYS), now, SOCIAL_SOURCE_TYPES)
    aliases = cfg.entity_aliases()
    rows = 0
    stats: dict[str, Any] = {"status": "ok"}
    for spec in cfg.claim_vs_reported:
        claimed = _claimed(spec, pages, llm)
        reported = _reported(spec, posts, llm)
        per_entity: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
        docs_by_id = {d.id: d for d in posts}
        for doc_id, rep in reported.items():
            d = docs_by_id[doc_id]
            ents = find_entities(rep["excerpt"], aliases) or list(d.entities or [])
            if len(set(ents)) != 1:
                continue  # ambiguous attribution: skip rather than guess
            entity = ents[0]
            lo, hi = spec.plausible_range or (None, None)
            cl = claimed.get(entity, (None, None, ""))[0]
            if lo is None and cl:
                lo, hi = 0.1 * cl, 2.5 * cl
            v = rep["value"]
            if (lo is not None and v < lo) or (hi is not None and v > hi):
                continue
            per_entity[entity].append((doc_id, rep))
        entities = sorted(set(per_entity) | set(claimed))
        for ent in entities:
            reps = sorted(per_entity.get(ent, []), key=lambda x: x[0])
            values = [r["value"] for _, r in reps]
            flags: list[str] = []
            med = q1 = q3 = None
            if values:
                med, q1, q3 = iqr_stats(values)
                if q3 - q1 > 0.5 * med:
                    flags.append("wide_iqr")
            if len(values) < spec.min_n:
                flags.append("low_confidence")
            cl_val, cl_src, cl_excerpt = claimed.get(ent, (None, None, ""))
            if cl_val is None:
                flags.append("no_claim_found")
            s.add(
                ClaimVsReported(
                    workspace=cfg.workspace,
                    run_id=run_id,
                    metric=spec.metric,
                    unit=spec.unit,
                    entity=ent,
                    claimed_value=cl_val,
                    claimed_source_id=cl_src,
                    claimed_excerpt=cl_excerpt or "",
                    reported_median=round(med, 1) if med is not None else None,
                    reported_q1=round(q1, 1) if q1 is not None else None,
                    reported_q3=round(q3, 1) if q3 is not None else None,
                    reported_iqr=round(q3 - q1, 1) if (q1 is not None and q3 is not None) else None,
                    n=len(values),
                    flags=flags,
                    reported_document_ids=[d for d, _ in reps],
                    reported_values=[{"document_id": d, **r} for d, r in reps],
                )
            )
            rows += 1
        stats[spec.metric] = {"claimed": len(claimed), "reported": len(reported), "entities": len(entities)}
    s.flush()
    stats["rows"] = rows
    return stats
