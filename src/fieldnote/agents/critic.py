"""Critic agent: gatekeeper for every claim that can reach an output.

Stage 1 (deterministic):
  * the cited sources exist (in this workspace);
  * the excerpt appears in a source (exact, or RapidFuzz partial ratio >= 85);
  * every number in the claim appears in the excerpt, or is derivable by the stated arithmetic
    (re-computed here), or matches FieldNote's own computed tables (claim-vs-reported, themes);
  * every named competitor appears in the excerpt / source titles / owning source, and no unknown
    proper names are introduced;
  * time-sensitive claims do not rest only on stale sources;
  * low-confidence comparisons are labelled as such; injected instructions are rejected.
Stage 2 (LLM): entailment label per claim (supported | partially_supported | unsupported |
  contradicted) with a one-line reason; overreach check for analysis claims; counter-evidence search
  per finding. Every decision is written to ``claims.checks``.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from fieldnote.agents.context import AgentContext
from fieldnote.costs import BudgetExceededError
from fieldnote.db import repo
from fieldnote.db.models import ClaimVsReported, Document, Theme
from fieldnote.llm.base import LLMError
from fieldnote.llm.schemas import CounterEvidenceOutput, EntailmentOutput
from fieldnote.logging_setup import get_logger
from fieldnote.processing.metrics import metric_summaries
from fieldnote.textutil import (
    UNTRUSTED_NOTICE,
    direction_of,
    extract_numbers,
    find_entities,
    fmt_number,
    looks_like_injection,
    normalize_ws,
    number_supported,
    proper_nouns,
    tokenize,
    truncate_words,
    wrap_untrusted,
)

log = get_logger(__name__)

EXCERPT_THRESHOLD = 85.0
STALE_DAYS = 45
TIME_SENSITIVE_TAGS = {"price", "launch", "sales", "metric", "change", "policy", "dealer", "feature", "share"}
STAGE2_BATCH = 10
_TIME_WORDS = re.compile(r"(?i)\b(now|currently|this week|latest|recently|today|new)\b")


class ClaimLike(Protocol):
    id: int
    type: str
    text: str
    source_document_ids: list[Any]
    excerpt: str
    entities: list[Any]
    tags: list[Any]
    derivation: dict[str, Any]
    depends_on_claim_ids: list[Any]
    status: str
    checks: dict[str, Any]
    critic_notes: str
    run_id: int | None


@dataclass
class Stage1Result:
    ok: bool
    checks: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    evidence: str = ""


def _norm(text: str) -> str:
    t = normalize_ws(text).lower()
    t = t.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'").replace("–", "-").replace("—", "-")
    return t.strip(" .\"'…")


def excerpt_match(excerpt: str, docs: list[Document]) -> tuple[bool, float, int | None]:
    ex = _norm(excerpt)
    if not ex:
        return False, 0.0, None
    best: tuple[float, int | None] = (0.0, None)
    for d in docs:
        hay = _norm(f"{d.title}\n{d.content or d.summary}")
        if ex in hay:
            return True, 100.0, d.id
        if len(ex) > 12:
            ratio = float(fuzz.partial_ratio(ex, hay)) if len(hay) < 200_000 else 0.0
            if ratio > best[0]:
                best = (ratio, d.id)
    return best[0] >= EXCERPT_THRESHOLD, best[0], best[1]


def _op_result(op: str, operands: list[float]) -> float | None:
    try:
        if op == "pct_change" and len(operands) == 2 and operands[0]:
            return (operands[1] - operands[0]) / abs(operands[0]) * 100.0
        if op == "difference" and len(operands) == 2:
            return operands[1] - operands[0]
        if op == "ratio" and len(operands) == 2 and operands[1]:
            return operands[0] / operands[1]
        if op == "share" and len(operands) == 2 and operands[1]:
            return operands[0] / operands[1] * 100.0
        if op == "sum" and operands:
            return float(sum(operands))
        if op == "median" and operands:
            return float(statistics.median(operands))
        if op == "count":
            return float(len(operands))
    except (TypeError, ZeroDivisionError):
        return None
    return None


class Critic:
    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx
        cfg = ctx.cfg
        self.aliases = cfg.entity_aliases()
        vocab = {cfg.region, cfg.sector, cfg.display_name, *cfg.aspects, *(c.metric for c in cfg.claim_vs_reported)}
        vocab |= {w for v in list(vocab) for w in re.split(r"[\s_,/-]+", v)}
        vocab |= {"FieldNote", "Estimate", "Reddit", "YouTube", "Google", "News", "IQR", "MoM"}
        self.allowed_words = {v.lower() for v in vocab if v}
        self._extra_words: set[str] = set()
        self._theme_row: dict[str, Any] | None = None

    # ------------------------------------------------------------------------------------------
    # Reference data (FieldNote's own computed tables)
    # ------------------------------------------------------------------------------------------
    def _reference(self, s: Session, claim: ClaimLike) -> tuple[bool, list[float], set[str], str, list[str]]:
        """Returns (found, numbers, entities, statement, flags) for a derivation reference."""
        der = claim.derivation or {}
        ref = der.get("reference") or {}
        table = ref.get("table")
        ws = self.ctx.cfg.workspace
        if table == "claim_vs_reported":
            q = select(ClaimVsReported).where(
                ClaimVsReported.workspace == ws,
                ClaimVsReported.metric == ref.get("metric"),
                ClaimVsReported.entity == ref.get("entity"),
            )
            if claim.run_id is not None:
                q = q.where(ClaimVsReported.run_id == claim.run_id)
            row = s.scalar(q.order_by(ClaimVsReported.id.desc()).limit(1))
            if row is None:
                return False, [], set(), "", []
            nums = [
                v
                for v in (row.reported_median, row.reported_q1, row.reported_q3, row.claimed_value, row.reported_iqr)
                if v is not None
            ]
            nums.append(float(row.n))
            if row.claimed_value and row.reported_median is not None:
                gap = (row.claimed_value - row.reported_median) / row.claimed_value * 100
                nums.append(gap)
                rel = "below" if gap > 0 else "above"
            else:
                gap, rel = 0.0, "vs"
            low = "low_confidence" in (row.flags or [])
            statement = (
                f"FieldNote claim-vs-reported table: metric {row.metric} ({row.unit}) for {row.entity}: owner-reported "
                f"median {row.reported_median} {row.unit}, IQR {row.reported_q1}-{row.reported_q3}, n={row.n}; claimed "
                f"{row.claimed_value} {row.unit}; reported median is {abs(gap):.0f}% {rel} the claim"
                f"{'; LOW CONFIDENCE (n below minimum)' if low else ''}."
            )
            return True, nums, {row.entity}, statement, list(row.flags or [])
        if table == "themes":
            try:
                theme_id = int(ref.get("theme_id", "0"))
            except ValueError:
                return False, [], set(), "", []
            t = s.get(Theme, theme_id)
            if t is None or t.workspace != ws:
                return False, [], set(), "", []
            ent_counts = dict(t.entity_distribution or {})
            nums = [
                float(t.size),
                float(t.size_prev),
                float(t.sentiment_mean),
                *[float(v) for v in ent_counts.values()],
            ]
            if t.wow_change is not None:
                nums.append(t.wow_change * 100)
            mentions = "; ".join(
                f"{v} of them mention {k}" for k, v in sorted(ent_counts.items(), key=lambda kv: -kv[1])
            )
            trend = "up" if t.size > t.size_prev else "down" if t.size < t.size_prev else "unchanged"
            statement = (
                f"FieldNote theme table: {t.size} customer posts in the current window clustered under the theme "
                f"'{t.label}', {trend} from {t.size_prev} in the previous window, mean sentiment {t.sentiment_mean:+.2f}"
                f"{', emerging' if t.is_emerging else ''}. {mentions}."
            )
            label_words = {w.lower() for w in re.findall(r"[A-Za-z]+", t.label)}
            self._extra_words = label_words
            self._theme_row = {"trend": trend, "entities": ent_counts, "size_prev": t.size_prev}
            return True, nums, set(ent_counts), statement, []
        return False, [], set(), "", []

    def _derivation(self, claim: ClaimLike, docs: list[Document]) -> tuple[bool, list[float], str]:
        der = claim.derivation or {}
        op = der.get("op", "none")
        operands = [float(x) for x in der.get("operands") or []]
        if op in ("none", "reference") or not operands:
            return True, [], ""
        source_nums = [n for d in docs for n in extract_numbers(f"{d.title}\n{d.content}", skip_dates=False)]
        missing = [x for x in operands if not any(abs(n.value - x) <= max(0.5, 1e-6 * abs(x)) for n in source_nums)]
        if missing:
            return False, [], f"derivation operands not found in sources: {missing[:3]}"
        expected = _op_result(op, operands)
        result = der.get("result")
        if expected is None:
            return False, [], f"cannot recompute op '{op}'"
        if result is not None and abs(float(result) - expected) > max(0.15, 0.005 * abs(expected)):
            return False, [], f"stated result {result} != recomputed {expected:.2f}"
        verb = "rose" if expected > 0 else "fell" if expected < 0 else "was flat"
        note = f"Recomputed by FieldNote: {op} of {', '.join(fmt_number(x, 1 if x % 1 else 0) for x in operands)} = {expected:.2f}"
        if op in ("pct_change", "difference"):
            note += f" ({verb})"
        return True, [*operands, expected], note

    # ------------------------------------------------------------------------------------------
    # Stage 1
    # ------------------------------------------------------------------------------------------
    def stage1(self, s: Session, claim: ClaimLike, docs_cache: dict[int, Document] | None = None) -> Stage1Result:
        self._extra_words = set()
        self._theme_row = None
        res = Stage1Result(ok=True)
        text = claim.text or ""
        ids = [int(i) for i in (claim.source_document_ids or []) if str(i).lstrip("-").isdigit()]
        docs_map = {i: docs_cache[i] for i in ids if docs_cache and i in docs_cache}
        missing_ids = [i for i in ids if i not in docs_map]
        if missing_ids:
            docs_map.update(repo.get_documents(s, missing_ids))
        docs = [d for i in ids if (d := docs_map.get(i)) is not None and d.workspace == self.ctx.cfg.workspace]
        res.checks["sources_found"] = len(docs)
        if not ids or len(docs) != len(ids):
            res.ok = False
            res.reasons.append("source missing" if ids else "no source cited")
        if looks_like_injection(text) or looks_like_injection(claim.excerpt or ""):
            res.ok = False
            res.reasons.append("claim contains instruction-like text (possible prompt injection)")
        # Excerpt
        excerpt = claim.excerpt or ""
        if claim.type == "fact" or excerpt:
            found, ratio, doc_id = excerpt_match(excerpt, docs)
            res.checks["excerpt_ratio"] = round(ratio, 1)
            res.checks["excerpt_doc"] = doc_id
            if not found:
                res.ok = False
                res.reasons.append(f"excerpt not found in source (best match {ratio:.0f}%)")
        # Reference / derivation
        ref_nums: list[float] = []
        ref_ents: set[str] = set()
        statement = ""
        flags: list[str] = []
        der = claim.derivation or {}
        if der.get("op") == "reference":
            found_ref, ref_nums, ref_ents, statement, flags = self._reference(s, claim)
            res.checks["reference_found"] = found_ref
            if not found_ref:
                res.ok = False
                res.reasons.append("referenced FieldNote data not found")
            elif self._theme_row is not None:
                # "N of them mention X" must match the stored per-entity count; "up/down from" the stored trend.
                theme_counts: dict[str, Any] = self._theme_row["entities"]
                for n_str, ent in re.findall(r"(\d+) of them mention ([A-Z][\w .&-]*?)(?=[;.,)]|\s\(|$)", text):
                    if int(theme_counts.get(ent.strip(), -1)) != int(n_str):
                        res.ok = False
                        res.reasons.append(f"theme mention count for {ent.strip()} does not match FieldNote data")
                m_tr = re.search(r"\b(up|down)\s+from\s+(\d+)", text)
                if m_tr and (
                    m_tr.group(1) != self._theme_row["trend"] or int(m_tr.group(2)) != self._theme_row["size_prev"]
                ):
                    res.ok = False
                    res.reasons.append("theme trend contradicts FieldNote data")
        der_ok, der_nums, der_note = self._derivation(claim, docs)
        if not der_ok:
            res.ok = False
            res.reasons.append(der_note)
        elif der_note:
            statement = f"{statement} {der_note}".strip()
            if der.get("op") in ("pct_change", "difference") and len(der_nums) >= 3:
                sign = (der_nums[-1] > 0) - (der_nums[-1] < 0)
                dirn = direction_of(text)
                if dirn and sign and dirn != sign:
                    res.ok = False
                    res.reasons.append("claim direction contradicts the recomputed change")
        if claim.type == "estimate" and not der:
            res.ok = False
            res.reasons.append("estimate without a stated method")
        if claim.type == "estimate" and not text.lower().startswith("estimate"):
            res.checks["label_added"] = True
        if "low_confidence" in flags and "low confidence" not in text.lower():
            res.ok = False
            res.reasons.append("comparison is low-confidence (n below minimum) but not labelled as such")
        # Numbers
        allowed = extract_numbers(excerpt, skip_dates=False)
        extra_vals = [*ref_nums, *der_nums]
        bad_numbers = [
            n.raw
            for n in extract_numbers(text)
            if not number_supported(n, allowed) and not number_supported(n, extra_vals)
        ]
        res.checks["unsupported_numbers"] = bad_numbers
        if bad_numbers:
            res.ok = False
            res.reasons.append(f"numbers not supported by excerpt or derivation: {', '.join(bad_numbers[:4])}")
        # Entities
        support_text = " ".join([excerpt, *[d.title for d in docs]])
        support_ents = set(find_entities(support_text, self.aliases)) | ref_ents
        for d in docs:
            owner = (d.meta or {}).get("competitor")
            if owner and d.is_primary:
                support_ents.add(owner)
        claim_ents = set(find_entities(text, self.aliases))
        # Entity metadata may only name entities the claim text itself mentions (or its reference data).
        listed = [str(e) for e in (claim.entities or [])]
        kept = [e for e in listed if e in claim_ents or e in ref_ents]
        if kept != listed:
            res.checks["entities_dropped"] = sorted(set(listed) - set(kept))
            claim.entities = kept
        bad_ents = sorted(claim_ents - support_ents)
        res.checks["unsupported_entities"] = bad_ents
        if bad_ents:
            res.ok = False
            res.reasons.append(f"entities not in excerpt/source: {', '.join(bad_ents)}")
        # Unknown proper names (fabrication guard)
        haystack = " ".join([excerpt, statement, *[f"{d.title} {d.content}" for d in docs]]).lower()
        alias_words = {w.lower() for a in self.aliases for w in a.split()}
        unknown = [
            p
            for p in proper_nouns(text)
            if p.lower() not in haystack
            and p.lower() not in self.allowed_words
            and p.lower() not in alias_words
            and p.lower() not in self._extra_words
        ]
        res.checks["unknown_names"] = unknown
        if unknown:
            res.ok = False
            res.reasons.append(f"names not found in sources: {', '.join(unknown[:3])}")
        # Staleness
        tags = set(claim.tags or [])
        if docs and (tags & TIME_SENSITIVE_TAGS or _TIME_WORDS.search(text)):
            newest = max((d.published_at or d.fetched_at) for d in docs)
            age = (self.ctx.now - newest).days
            res.checks["newest_source_age_days"] = age
            if age > STALE_DAYS:
                res.ok = False
                res.reasons.append(f"time-sensitive claim relies on sources {age} days old")
        res.evidence = (excerpt + ("\n" + statement if statement else "")).strip()
        res.checks["stage1"] = "pass" if res.ok else "fail"
        return res

    # ------------------------------------------------------------------------------------------
    # Stage 2
    # ------------------------------------------------------------------------------------------
    def _entailment(self, items: list[dict[str, Any]], task: str = "critic_entailment") -> dict[int, tuple[str, str]]:
        out: dict[int, tuple[str, str]] = {}
        system = (
            "You are the Critic in FieldNote. For each claim, judge ONLY against the evidence shown: supported, "
            "partially_supported, unsupported, or contradicted, with a one-line reason. Numbers, entities and "
            f"direction of change must match exactly. {UNTRUSTED_NOTICE}"
        )
        if task == "critic_overreach":
            system = (
                "You are the Critic in FieldNote. Each analysis claim must follow from its listed dependency claims "
                "without overreaching (no new facts, numbers, entities, certainty or causal leaps). Label supported, "
                f"partially_supported, unsupported or contradicted with a one-line reason. {UNTRUSTED_NOTICE}"
            )
        for i in range(0, len(items), STAGE2_BATCH):
            batch = items[i : i + STAGE2_BATCH]
            if task == "critic_overreach":
                prompt = "\n\n".join(
                    f"[claim {it['claim_id']}] ANALYSIS: {it['claim']}\nDEPENDENCIES:\n"
                    + "\n".join(f"- {d}" for d in it["dependencies"])
                    for it in batch
                )
            else:
                prompt = "\n\n".join(
                    f"[claim {it['claim_id']}] CLAIM: {it['claim']}\nEVIDENCE: {wrap_untrusted(it['evidence'])}"
                    for it in batch
                )
            res = self.ctx.llm.structured(
                task=task,
                system=system,
                prompt=prompt,
                schema=EntailmentOutput,
                tier="main",
                payload={"items": batch},
            )
            for j in res.judgements:
                out[j.claim_id] = (j.label, j.reason)
        return out

    def verify_claims(self, s: Session, claims: list[Any], *, persist: bool = True) -> dict[str, int]:
        stats = {"checked": len(claims), "stage1_failed": 0, "verified": 0, "rejected": 0, "unverified": 0}
        doc_ids = {int(i) for c in claims for i in (c.source_document_ids or []) if str(i).lstrip("-").isdigit()}
        docs_cache = repo.get_documents(s, doc_ids)
        pending: list[dict[str, Any]] = []
        by_id: dict[int, Any] = {}
        for c in claims:
            r = self.stage1(s, c, docs_cache)
            c.checks = {**(c.checks or {}), **r.checks, "stage1_reasons": r.reasons}
            by_id[c.id] = c
            if not r.ok:
                c.status = "rejected"
                c.critic_notes = "; ".join(r.reasons)[:1000]
                stats["stage1_failed"] += 1
                stats["rejected"] += 1
                continue
            pending.append({"claim_id": c.id, "claim": c.text, "evidence": r.evidence, "type": c.type})
        if pending:
            try:
                labels = self._entailment(pending)
            except (LLMError, BudgetExceededError) as exc:
                log.warning("critic stage 2 unavailable: %s", exc)
                labels = {}
                for it in pending:
                    c = by_id[it["claim_id"]]
                    c.status = "unverified"
                    c.checks = {**c.checks, "stage2": "unavailable"}
                    c.critic_notes = "stage 2 unavailable; claim withheld from outputs"
                    stats["unverified"] += 1
                if isinstance(exc, BudgetExceededError):
                    raise
            for it in pending:
                c = by_id[it["claim_id"]]
                if it["claim_id"] not in labels:
                    if c.checks.get("stage2") != "unavailable":
                        c.status = "unverified"
                        c.checks = {**c.checks, "stage2": "missing"}
                        stats["unverified"] += 1
                    continue
                label, reason = labels[it["claim_id"]]
                accept = label == "supported" or (label == "partially_supported" and c.type == "estimate")
                c.status = "verified" if accept else "rejected"
                c.checks = {**c.checks, "stage2": label, "stage2_reason": reason}
                c.critic_notes = reason[:1000]
                stats["verified" if accept else "rejected"] += 1
        if persist:
            s.flush()
        return stats

    def verify_analysis(self, s: Session, analysis: list[Any], deps: dict[int, Any]) -> dict[str, int]:
        stats = {"checked": len(analysis), "verified": 0, "rejected": 0, "unverified": 0}
        pending = []
        for a in analysis:
            reasons = []
            dep_claims = [deps[i] for i in (a.depends_on_claim_ids or []) if i in deps]
            if not dep_claims or any(d.status != "verified" for d in dep_claims):
                reasons.append("dependencies missing or unverified")
            dep_text = " ".join(d.text for d in dep_claims)
            dep_nums = extract_numbers(dep_text, skip_dates=False)
            bad_nums = [n.raw for n in extract_numbers(a.text) if not number_supported(n, dep_nums)]
            if bad_nums:
                reasons.append(f"numbers not in dependencies: {', '.join(bad_nums[:3])}")
            dep_ents = set(find_entities(dep_text, self.aliases)) | {e for d in dep_claims for e in (d.entities or [])}
            bad_ents = sorted(set(find_entities(a.text, self.aliases)) - dep_ents)
            if bad_ents:
                reasons.append(f"entities not in dependencies: {', '.join(bad_ents)}")
            if looks_like_injection(a.text):
                reasons.append("instruction-like text")
            a.checks = {**(a.checks or {}), "stage1": "fail" if reasons else "pass", "stage1_reasons": reasons}
            if reasons:
                a.status = "rejected"
                a.critic_notes = "; ".join(reasons)
                stats["rejected"] += 1
                continue
            pending.append({"claim_id": a.id, "claim": a.text, "dependencies": [d.text for d in dep_claims]})
        if pending:
            try:
                labels = self._entailment(pending, task="critic_overreach")
            except (LLMError, BudgetExceededError) as exc:
                log.warning("critic overreach check unavailable: %s", exc)
                labels = {}
                if isinstance(exc, BudgetExceededError):
                    for it in pending:
                        a = next(x for x in analysis if x.id == it["claim_id"])
                        a.status = "unverified"
                    raise
            for it in pending:
                a = next(x for x in analysis if x.id == it["claim_id"])
                if it["claim_id"] not in labels:
                    a.status = "unverified"
                    stats["unverified"] += 1
                    continue
                label, reason = labels[it["claim_id"]]
                a.status = "verified" if label == "supported" else "rejected"
                a.checks = {**a.checks, "stage2": label, "stage2_reason": reason}
                a.critic_notes = reason
                stats["verified" if label == "supported" else "rejected"] += 1
        s.flush()
        return stats

    # ------------------------------------------------------------------------------------------
    # Counter-evidence
    # ------------------------------------------------------------------------------------------
    def counter_evidence(self, title: str, entities: list[str], evidence: list[Any]) -> list[dict[str, Any]]:
        exclude = {int(i) for c in evidence for i in (c.source_document_ids or [])}
        query = " ".join([title, *entities])
        hits = self.ctx.tools.dispatch("search_documents", {"query": query[:280], "k": 10}).get("results", [])
        candidates = [h["id"] for h in hits if h["id"] not in exclude][:4]
        if not candidates:
            return []
        with self.ctx.db.session() as s:
            docs = repo.get_documents(s, candidates)
            texts = {i: f"{d.title}\n{truncate_words(d.content or d.summary, 400)}" for i, d in docs.items()}
        directions = [direction_of(c.text) for c in evidence]
        direction = (sum(directions) > 0) - (sum(directions) < 0)
        topic = sorted(
            {t for c in evidence for t in tokenize(c.text, keep_numbers=False)}
            | {t for t in tokenize(title, keep_numbers=False)}
        )
        res = self.ctx.llm.structured(
            task="counter_evidence",
            system=(
                "You check whether any document contradicts a finding. Label each document contradicts, supports or "
                f"unrelated, with a short note quoting at most 25 words. {UNTRUSTED_NOTICE}"
            ),
            prompt=f"FINDING: {title}\nEVIDENCE: "
            + " | ".join(c.text for c in evidence[:5])
            + "\n\n"
            + "\n\n".join(f"[doc {i}] {wrap_untrusted(t)}" for i, t in texts.items()),
            schema=CounterEvidenceOutput,
            tier="fast",
            payload={
                "finding": {"title": title, "entities": entities, "direction": direction, "topic_tokens": topic},
                "documents": [{"id": i, "text": t} for i, t in texts.items()],
            },
        )
        return [j.model_dump() for j in res.judgements if j.document_id in texts]


def metric_entity_shares(ctx: AgentContext) -> tuple[dict[str, float], str]:
    with ctx.db.session() as s:
        summaries = metric_summaries(s, ctx.cfg)
    if not summaries:
        return {}, "volumes"
    summ = summaries[0]
    return {e.entity: round(e.share or 0.0, 2) for e in summ.entities}, summ.label


def is_stale(ctx: AgentContext, days: int) -> Any:
    return ctx.now - timedelta(days=days)
