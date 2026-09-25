"""Claim-level evals: citation validity, adversarial critic catch rate, output number consistency."""

from __future__ import annotations

import random
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from fieldnote.agents.context import AgentContext
from fieldnote.agents.critic import Critic, excerpt_match
from fieldnote.config import WorkspaceConfig
from fieldnote.db import repo
from fieldnote.db.models import Claim, Finding
from fieldnote.textutil import (
    DIRECTION_PAIRS,
    extract_numbers,
    find_entities,
    fmt_number,
    number_supported,
    opposite_direction,
)

CORRUPTIONS = ("change_number", "swap_entity", "fabricate_fact", "flip_direction")


# ---------------------------------------------------------------------------------------------
# 1. Citation validity
# ---------------------------------------------------------------------------------------------


def citation_validity(s: Session, workspace: str, run_id: int) -> dict[str, Any]:
    claims = [c for c in repo.claims_for_run(s, workspace, run_id) if c.type in ("fact", "estimate")]
    docs = repo.get_documents(s, {int(i) for c in claims for i in (c.source_document_ids or [])})
    valid = 0
    failures = []
    for c in claims:
        srcs = [docs.get(int(i)) for i in (c.source_document_ids or [])]
        ok = bool(srcs) and all(d is not None for d in srcs)
        if ok and c.excerpt:
            ok = excerpt_match(c.excerpt, [d for d in srcs if d is not None])[0]
        elif ok and c.type == "fact":
            ok = False
        valid += int(ok)
        if not ok:
            failures.append(c.id)
    n = len(claims)
    return {"claims": n, "valid": valid, "rate": round(valid / n, 4) if n else 1.0, "failures": failures[:20]}


# ---------------------------------------------------------------------------------------------
# 2. Adversarial critic catch rate
# ---------------------------------------------------------------------------------------------


@dataclass
class CorruptedClaim:
    kind: str
    original_id: int
    claim: Claim


@dataclass
class AdversarialResult:
    total: int = 0
    caught: int = 0
    by_kind: dict[str, dict[str, int]] = field(default_factory=lambda: defaultdict(lambda: {"total": 0, "caught": 0}))
    missed_examples: list[dict[str, str]] = field(default_factory=list)
    base_verified: int = 0

    @property
    def rate(self) -> float:
        return round(self.caught / self.total, 4) if self.total else 1.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "caught": self.caught,
            "rate": self.rate,
            "by_kind": {
                k: {**v, "rate": round(v["caught"] / v["total"], 4) if v["total"] else None}
                for k, v in self.by_kind.items()
            },
            "missed_examples": self.missed_examples[:8],
            "base_verified": self.base_verified,
        }


def _copy_claim(c: Claim, new_id: int, **changes: Any) -> Claim:
    fields = {
        "id": new_id,
        "workspace": c.workspace,
        "run_id": c.run_id,
        "agent": "eval",
        "type": c.type,
        "text": c.text,
        "source_document_ids": list(c.source_document_ids or []),
        "excerpt": c.excerpt,
        "entities": list(c.entities or []),
        "tags": list(c.tags or []),
        "aspect": c.aspect,
        "derivation": dict(c.derivation or {}),
        "depends_on_claim_ids": [],
        "status": "unverified",
        "checks": {},
        "critic_notes": "",
    }
    fields.update(changes)
    return Claim(**fields)


def _change_number(text: str) -> str | None:
    nums = [n for n in extract_numbers(text) if abs(n.value) >= 1]
    if not nums:
        return None
    n = nums[-1] if len(nums) > 1 else nums[0]
    new_val = abs(n.value) * 1.37 + 3
    decimals = n.decimals
    body = fmt_number(new_val / n.multiplier, decimals) if n.multiplier != 1 else fmt_number(new_val, decimals)
    raw_num = re.search(r"\d[\d,]*(?:\.\d+)?", n.raw)
    if raw_num is None:
        return None
    new_raw = n.raw.replace(raw_num.group(0), body, 1)
    return text[: n.start] + text[n.start :].replace(n.raw, new_raw, 1)


def _swap_entity(text: str, cfg: WorkspaceConfig) -> str | None:
    aliases = cfg.entity_aliases()
    ents = find_entities(text, aliases)
    if not ents:
        return None
    names = cfg.competitor_names()
    src = ents[0]
    target = next(n for n in names[names.index(src) + 1 :] + names if n != src and n not in ents)
    out = text
    for alias, canon in sorted(aliases.items(), key=lambda kv: -len(kv[0])):
        if canon == src:
            out = re.sub(rf"(?<![\w]){re.escape(alias)}(?![\w])", target, out, flags=re.IGNORECASE)
    return out if out != text else None


def _flip_direction(text: str) -> str | None:
    words = re.findall(r"[A-Za-z']+", text)
    for w in words:
        opp = opposite_direction(w)
        if opp and (w.lower() in DIRECTION_PAIRS or w.lower() in DIRECTION_PAIRS.values()):
            repl = opp.capitalize() if w[0].isupper() else opp
            return re.sub(rf"\b{re.escape(w)}\b", repl, text, count=1)
    return None


def build_corruptions(claims: list[Claim], cfg: WorkspaceConfig, seed: int = 7) -> list[CorruptedClaim]:
    rng = random.Random(seed)
    out: list[CorruptedClaim] = []
    next_id = -1
    all_doc_ids = sorted({int(i) for c in claims for i in (c.source_document_ids or [])})
    for c in claims:
        variants: list[tuple[str, dict[str, Any]]] = []
        t = _change_number(c.text)
        if t:
            variants.append(("change_number", {"text": t}))
        t = _swap_entity(c.text, cfg)
        if t:
            variants.append(("swap_entity", {"text": t}))
        t = _flip_direction(c.text)
        if t:
            variants.append(("flip_direction", {"text": t}))
        ents = cfg.competitor_names()
        ent = (c.entities or [ents[0]])[0]
        fabricated = f"{ent} announced a new plant with annual capacity of {rng.randint(2, 9)},00,000 units."
        other_docs = [d for d in all_doc_ids if d not in (c.source_document_ids or [])] or all_doc_ids
        variants.append(
            (
                "fabricate_fact",
                {
                    "text": fabricated,
                    "excerpt": fabricated,
                    "type": "fact",
                    "derivation": {},
                    "source_document_ids": [rng.choice(other_docs)] if other_docs else [],
                },
            )
        )
        for kind, changes in variants:
            out.append(CorruptedClaim(kind=kind, original_id=c.id, claim=_copy_claim(c, next_id, **changes)))
            next_id -= 1
    return out


def adversarial_catch_rate(
    ctx: AgentContext, s: Session, base_claims: list[Claim], max_claims: int = 40
) -> AdversarialResult:
    base = [c for c in base_claims if c.status == "verified" and c.type in ("fact", "estimate")][:max_claims]
    res = AdversarialResult(base_verified=len(base))
    corrupted = build_corruptions(base, ctx.cfg)
    if not corrupted:
        return res
    critic = Critic(ctx)
    objs = [cc.claim for cc in corrupted]
    critic.verify_claims(s, objs, persist=False)
    for cc in corrupted:
        caught = cc.claim.status != "verified"
        res.total += 1
        res.caught += int(caught)
        res.by_kind[cc.kind]["total"] += 1
        res.by_kind[cc.kind]["caught"] += int(caught)
        if not caught:
            res.missed_examples.append({"kind": cc.kind, "text": cc.claim.text[:200]})
    for obj in objs:  # never persist eval claims
        if obj in s:
            s.expunge(obj)
    return res


# ---------------------------------------------------------------------------------------------
# 3. Output number consistency
# ---------------------------------------------------------------------------------------------

_SECTION = re.compile(r"<!-- fn:section (?P<attrs>[^>]*?)-->(?P<body>.*?)<!-- fn:end -->", re.S)
_LINK_TARGET = re.compile(r"\]\([^)]*\)")
_MORE = re.compile(r"\+\d+ more")
_CHIP_LINE = re.compile(r"(?m)^`[^`]*`.*$")
_HEADING_NUM = re.compile(r"(?m)^(#+\s*)\d+\.\s")
_BOLD_MARK = re.compile(r"\*\*[^*]+\*\*")


def _attrs(s: str) -> dict[str, str]:
    return dict(re.findall(r"(\w+)=(\S+)", s))


def _clean_body(body: str) -> str:
    body = _LINK_TARGET.sub("]", body)
    body = _MORE.sub("", body)
    body = _CHIP_LINE.sub("", body)
    body = _HEADING_NUM.sub(r"\1", body)
    return body


def number_consistency(s: Session, cfg: WorkspaceConfig, md_paths: list[Path], run_id: int) -> dict[str, Any]:
    """Every number and competitor name in finding sections must trace to that finding's verified claims.

    System sections are checked against the deterministic data they render (change events, themes,
    claim-vs-reported rows, metric summaries).
    """
    from fieldnote.processing.metrics import metric_summaries

    aliases = cfg.entity_aliases()
    checked = consistent = 0
    ent_checked = ent_ok = 0
    problems: list[dict[str, Any]] = []
    changes = repo.changes_for_run(s, cfg.workspace, run_id)
    themes = repo.themes_for_run(s, cfg.workspace, run_id)
    cvr = repo.cvr_for_run(s, cfg.workspace, run_id)
    system_values: list[float] = []
    system_texts: list[str] = [c.summary for c in changes]
    for t in themes:
        system_values += [t.size, t.size_prev, t.size - t.size_prev, t.sentiment_mean]
        system_texts.append(t.label)
    for r in cvr:
        system_values += [v for v in (r.reported_median, r.claimed_value, r.n) if v is not None]
    for summ in metric_summaries(s, cfg):
        for e in summ.entities:
            system_values += [e.latest, e.previous or 0, round(e.pct_change or 0, 1), round(e.share or 0, 1)]
    system_nums = [n for t in system_texts for n in extract_numbers(t, skip_dates=False)]
    for path in md_paths:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for m in _SECTION.finditer(text):
            attrs = _attrs(m.group("attrs"))
            body = _clean_body(m.group("body"))
            kind = attrs.get("kind")
            if kind == "finding":
                f = s.get(Finding, int(attrs.get("id", 0)))
                if f is None:
                    continue
                ids = list(f.evidence_claim_ids or []) + list(f.analysis_claim_ids or [])
                claims = [c for c in repo.get_claims(s, ids).values() if c.status == "verified"]
                allowed_text = " ".join([c.text for c in claims] + [c.excerpt for c in claims])
                allowed = extract_numbers(allowed_text, skip_dates=False)
                allowed_ents = set(find_entities(allowed_text, aliases)) | {
                    e for c in claims for e in (c.entities or [])
                }
                for n in extract_numbers(body, skip_timeframes=True):
                    checked += 1
                    if number_supported(n, allowed):
                        consistent += 1
                    else:
                        problems.append({"file": path.name, "section": f"finding {f.id}", "number": n.raw})
                for ent in find_entities(body, aliases):
                    ent_checked += 1
                    if ent in allowed_ents:
                        ent_ok += 1
                    else:
                        problems.append({"file": path.name, "section": f"finding {f.id}", "entity": ent})
            elif kind == "system" and attrs.get("name") in ("changes", "voice", "metrics"):
                for n in extract_numbers(body):
                    checked += 1
                    if number_supported(n, system_nums) or number_supported(n, system_values):
                        consistent += 1
                    else:
                        problems.append({"file": path.name, "section": attrs.get("name"), "number": n.raw})
    rate = round(consistent / checked, 4) if checked else 1.0
    ent_rate = round(ent_ok / ent_checked, 4) if ent_checked else 1.0
    return {
        "numbers_checked": checked,
        "numbers_consistent": consistent,
        "rate": rate,
        "entities_checked": ent_checked,
        "entity_rate": ent_rate,
        "problems": problems[:20],
    }
