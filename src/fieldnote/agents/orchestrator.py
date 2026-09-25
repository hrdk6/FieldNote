"""Explicit state machine for the agent pipeline (no framework lock-in).

build_context -> research -> critic_facts -> analyze -> critic_findings -> score -> write -> persist -> done

Each stage has typed inputs/outputs on :class:`PipelineState`, is traced to ``agent_traces``, and
degrades gracefully: a failure or an exhausted LLM budget is recorded in the stage log and the
pipeline continues with whatever verified output exists (deterministic fallbacks instead of LLM).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from rapidfuzz import fuzz
from sqlalchemy import delete

from fieldnote.agents.analyst import run_analyst
from fieldnote.agents.context import AgentContext, RunContextSummary, build_run_context
from fieldnote.agents.critic import Critic
from fieldnote.agents.researcher import run_researcher
from fieldnote.agents.writer import post_check, write_finding_prose
from fieldnote.costs import BudgetExceededError
from fieldnote.db import repo
from fieldnote.db.models import Claim, Finding
from fieldnote.domain import StageRecord
from fieldnote.llm.base import LLMError
from fieldnote.llm.schemas import FindingDraft, ResearchOutput
from fieldnote.logging_setup import get_logger
from fieldnote.scoring.opportunities import EvidenceSource, ScoreInput, score, sort_key
from fieldnote.textutil import find_entities

log = get_logger(__name__)


class Stage(StrEnum):
    BUILD_CONTEXT = "build_context"
    RESEARCH = "research"
    CRITIC_FACTS = "critic_facts"
    ANALYZE = "analyze"
    CRITIC_FINDINGS = "critic_findings"
    SCORE = "score"
    WRITE = "write"
    PERSIST = "persist"
    DONE = "done"


TRANSITIONS: dict[Stage, Stage] = {
    Stage.BUILD_CONTEXT: Stage.RESEARCH,
    Stage.RESEARCH: Stage.CRITIC_FACTS,
    Stage.CRITIC_FACTS: Stage.ANALYZE,
    Stage.ANALYZE: Stage.CRITIC_FINDINGS,
    Stage.CRITIC_FINDINGS: Stage.SCORE,
    Stage.SCORE: Stage.WRITE,
    Stage.WRITE: Stage.PERSIST,
    Stage.PERSIST: Stage.DONE,
}


@dataclass
class Candidate:
    draft: FindingDraft
    title: str
    evidence_ids: list[int]
    analysis_ids: list[int]
    status: str = "active"
    checks: dict[str, Any] = field(default_factory=dict)
    sources: list[EvidenceSource] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    evidence_breakdown: dict[str, float] = field(default_factory=dict)
    total: float = 0.0
    prose: dict[str, Any] = field(default_factory=dict)
    finding_id: int | None = None

    def raw_ratings(self) -> dict[str, dict[str, Any]]:
        d = self.draft
        return {
            "market_size": d.market_size.model_dump(),
            "competitor_gap": d.competitor_gap.model_dump(),
            "effort": d.effort.model_dump(),
        }


@dataclass
class PipelineState:
    stage: Stage = Stage.BUILD_CONTEXT
    summary: RunContextSummary | None = None
    research: ResearchOutput | None = None
    claim_ids: list[int] = field(default_factory=list)
    verified_ids: list[int] = field(default_factory=list)
    drafts: list[FindingDraft] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    finding_ids: list[int] = field(default_factory=list)
    disagreements: list[dict[str, Any]] = field(default_factory=list)
    stages: list[StageRecord] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    budget_exhausted: bool = False

    @property
    def degraded(self) -> bool:
        return self.budget_exhausted or any(s.status in ("failed", "degraded") for s in self.stages)


class AgentOrchestrator:
    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx
        self.critic = Critic(ctx)
        self.handlers = {
            Stage.BUILD_CONTEXT: self._build_context,
            Stage.RESEARCH: self._research,
            Stage.CRITIC_FACTS: self._critic_facts,
            Stage.ANALYZE: self._analyze,
            Stage.CRITIC_FINDINGS: self._critic_findings,
            Stage.SCORE: self._score,
            Stage.WRITE: self._write,
            Stage.PERSIST: self._persist,
        }

    def run(self) -> PipelineState:
        state = PipelineState()
        while state.stage != Stage.DONE:
            rec = StageRecord(name=str(state.stage))
            start = time.perf_counter()
            try:
                count = self.handlers[state.stage](state)
                rec.count = count
            except BudgetExceededError as exc:
                state.budget_exhausted = True
                rec.status = "degraded"
                rec.message = str(exc)
                log.warning("stage %s: %s", state.stage, exc)
            except (LLMError, Exception) as exc:  # a stage failure never aborts the run
                rec.status = "failed"
                rec.message = f"{type(exc).__name__}: {exc}"[:500]
                log.exception("agent stage %s failed", state.stage)
            rec.seconds = time.perf_counter() - start
            state.stages.append(rec)
            self.ctx.llm.cache.flush()
            self.ctx.tracer.flush()
            state.stage = TRANSITIONS[state.stage]
        state.stats.update(
            {
                "claims": len(state.claim_ids),
                "verified_claims": len(state.verified_ids),
                "findings": len(state.finding_ids),
                "budget_exhausted": state.budget_exhausted,
                "disagreements": state.disagreements,
                "stages": [s.as_dict() for s in state.stages],
            }
        )
        return state

    # ---- stages -----------------------------------------------------------------------------
    def _build_context(self, state: PipelineState) -> int:
        with self.ctx.tracer.step("orchestrator", "build_context") as tr:
            state.summary = build_run_context(self.ctx)
            tr.output_summary = state.summary.to_prompt()[:2000]
        return sum(state.summary.new_documents.values())

    def _research(self, state: PipelineState) -> int:
        if state.budget_exhausted or state.summary is None:
            return 0
        out = run_researcher(self.ctx, state.summary)
        state.research = out
        ref_to_id: dict[str, int] = {}
        with self.ctx.db.session() as s:
            for c in out.claims:
                row = Claim(
                    workspace=self.ctx.cfg.workspace,
                    run_id=self.ctx.run_id,
                    agent="researcher",
                    type=c.type,
                    text=c.text
                    if c.type != "estimate" or c.text.lower().startswith("estimate")
                    else f"Estimate: {c.text}",
                    source_document_ids=list(dict.fromkeys(c.source_document_ids)),
                    excerpt=c.excerpt,
                    entities=c.entities,
                    tags=c.tags,
                    aspect=c.aspect,
                    derivation=c.derivation.model_dump() if c.derivation else {},
                    ref=c.ref,
                    status="unverified",
                )
                s.add(row)
                s.flush()
                ref_to_id[c.ref] = row.id
                state.claim_ids.append(row.id)
        state.disagreements = [
            {
                "description": d.description,
                "claim_ids": [ref_to_id[r] for r in d.claim_refs if r in ref_to_id],
                "source_document_ids": d.source_document_ids,
            }
            for d in out.disagreements
        ]
        return len(state.claim_ids)

    def _critic_facts(self, state: PipelineState) -> int:
        if not state.claim_ids:
            return 0
        with (
            self.ctx.tracer.step("critic", "verify_facts", input_summary=f"{len(state.claim_ids)} claims") as tr,
            self.ctx.db.session() as s,
        ):
            claims = list(repo.get_claims(s, state.claim_ids).values())
            stats: dict[str, Any]
            try:
                stats = self.critic.verify_claims(s, claims)
            except BudgetExceededError:
                # Keep stage-1 decisions; claims without a stage-2 verdict stay unverified (withheld).
                state.budget_exhausted = True
                stats = {"note": "LLM budget exhausted during stage 2; unjudged claims withheld"}
            s.flush()
            state.verified_ids = sorted(c.id for c in claims if c.status == "verified")
            tr.output_summary = str(stats)
            state.stats["critic_facts"] = stats
            if state.disagreements:
                for c in claims:
                    if any(c.id in d["claim_ids"] for d in state.disagreements):
                        c.checks = {**(c.checks or {}), "disputed": True}
        return len(state.verified_ids)

    def _analyze(self, state: PipelineState) -> int:
        if not state.verified_ids or state.budget_exhausted:
            return 0
        with self.ctx.db.session() as s:
            claims = list(repo.get_claims(s, state.verified_ids).values())
            doc_ids = {int(i) for c in claims for i in c.source_document_ids}
            types = {i: d.source_type for i, d in repo.get_documents(s, doc_ids).items()}
        out = run_analyst(self.ctx, sorted(claims, key=lambda c: c.id), types)
        state.drafts = out.findings
        return len(state.drafts)

    def _critic_findings(self, state: PipelineState) -> int:
        if not state.drafts:
            return 0
        cfg = self.ctx.cfg
        min_ev = cfg.scoring.min_evidence_per_finding
        with (
            self.ctx.tracer.step("critic", "verify_findings", input_summary=f"{len(state.drafts)} drafts") as tr,
            self.ctx.db.session() as s,
        ):
            verified = repo.get_claims(s, state.verified_ids)
            analysis_rows: dict[int, list[Claim]] = {}
            all_analysis: list[Claim] = []
            for i, d in enumerate(state.drafts):
                rows = []
                for a in d.analysis_claims:
                    deps = [x for x in a.depends_on if x in verified]
                    src = sorted({int(sid) for x in deps for sid in verified[x].source_document_ids})
                    row = Claim(
                        workspace=cfg.workspace,
                        run_id=self.ctx.run_id,
                        agent="analyst",
                        type="analysis",
                        text=a.text,
                        source_document_ids=src,
                        depends_on_claim_ids=deps,
                        entities=sorted({e for x in deps for e in (verified[x].entities or [])}),
                        status="unverified",
                    )
                    s.add(row)
                    rows.append(row)
                s.flush()
                analysis_rows[i] = rows
                all_analysis += rows
            budget_hit = False
            stats: dict[str, Any]
            try:
                stats = self.critic.verify_analysis(s, all_analysis, verified)
            except BudgetExceededError:
                budget_hit = True
                stats = {"note": "budget exhausted during overreach check"}
            state.stats["critic_analysis"] = stats
            docs = repo.get_documents(s, {int(x) for c in verified.values() for x in c.source_document_ids})
            for i, d in enumerate(state.drafts):
                ev_ids = [x for x in dict.fromkeys(d.evidence_claim_ids) if x in verified]
                an_ids = [a.id for a in analysis_rows[i] if a.status == "verified"]
                checks: dict[str, Any] = {
                    "evidence_verified": len(ev_ids),
                    "analysis_verified": len(an_ids),
                    "analysis_rejected": sum(1 for a in analysis_rows[i] if a.status == "rejected"),
                }
                if len(ev_ids) >= min_ev:
                    status = "active"
                elif ev_ids:
                    status = "demoted"
                    checks["demoted_reason"] = f"only {len(ev_ids)} verified evidence claim(s); minimum is {min_ev}"
                else:
                    checks["dropped_reason"] = "no verified evidence"
                    continue
                allowed = [verified[x].text for x in ev_ids]
                mentioned = {e for t in allowed for e in find_entities(t, cfg.entity_aliases())}
                d.entities = [e for e in d.entities if e in mentioned]
                title = d.title
                if not post_check(title, allowed, cfg).ok:
                    ents = sorted({e for t in allowed for e in find_entities(t, cfg.entity_aliases())})
                    title = f"{'Opportunity' if d.category == 'opportunity' else 'Risk'}: {', '.join(ents) or 'market signal'}"
                    checks["title_replaced"] = True
                sources = []
                for x in ev_ids:
                    for sid in verified[x].source_document_ids:
                        doc = docs.get(int(sid))
                        if doc is not None:
                            sources.append(
                                EvidenceSource(
                                    doc.id, doc.source_type, doc.published_at or doc.fetched_at, doc.is_primary
                                )
                            )
                state.candidates.append(
                    Candidate(
                        draft=d,
                        title=title,
                        evidence_ids=ev_ids,
                        analysis_ids=an_ids,
                        status=status,
                        checks=checks,
                        sources=sources,
                    )
                )
            tr.output_summary = f"{len(state.candidates)} findings kept of {len(state.drafts)}"
        # Counter-evidence search (LLM second opinion) per surviving finding.
        if not budget_hit:
            with (
                self.ctx.tracer.step(
                    "critic", "counter_evidence", input_summary=f"{len(state.candidates)} findings"
                ) as tr,
                self.ctx.db.session() as s,
            ):
                verified = repo.get_claims(s, state.verified_ids)
                disputed = 0
                for cand in state.candidates:
                    try:
                        judgements = self.critic.counter_evidence(
                            cand.title, cand.draft.entities, [verified[x] for x in cand.evidence_ids]
                        )
                    except BudgetExceededError:
                        state.budget_exhausted = True
                        break
                    except LLMError as exc:
                        cand.checks["counter_evidence_error"] = str(exc)[:200]
                        continue
                    contra = [j for j in judgements if j["verdict"] == "contradicts"]
                    cand.checks["counter_evidence_checked"] = len(judgements)
                    if contra:
                        cand.checks["counter_evidence"] = contra
                        cand.checks["disputed"] = True
                        disputed += 1
                tr.output_summary = f"{disputed} findings with counter-evidence"
        else:
            state.budget_exhausted = True
        return len(state.candidates)

    def _score(self, state: PipelineState) -> int:
        cfg = self.ctx.cfg
        for cand in state.candidates:
            inp = ScoreInput(
                title=cand.title,
                ratings={
                    "market_size": cand.draft.market_size.value,
                    "competitor_gap": cand.draft.competitor_gap.value,
                    "effort": cand.draft.effort.value,
                },
                sources=cand.sources,
                evidence_count=len(cand.evidence_ids),
                last_seen=self.ctx.now,
            )
            res = score(inp, cfg.scoring.weights, self.ctx.now, cfg.scoring.evidence_half_life_days)
            cand.scores = res.scores
            cand.evidence_breakdown = res.evidence_breakdown
            cand.total = res.total
        state.candidates = _dedupe_candidates(state.candidates)
        state.candidates.sort(key=lambda c: sort_key(c.total, len(c.evidence_ids), self.ctx.now, c.title, 0))
        return len(state.candidates)

    def _write(self, state: PipelineState) -> int:
        cfg = self.ctx.cfg
        top_n = cfg.scoring.top_n_in_brief
        budget = max(40, cfg.output.brief_max_words // max(1, min(top_n, len(state.candidates) or 1)))
        written = 0
        with self.ctx.db.session() as s:
            claims = repo.get_claims(s, [x for c in state.candidates for x in (*c.evidence_ids, *c.analysis_ids)])
            for cand in state.candidates[: top_n * 2]:
                cl = [
                    {"id": x, "type": claims[x].type, "text": claims[x].text}
                    for x in (*cand.evidence_ids, *cand.analysis_ids)
                    if x in claims
                ]
                finding = {
                    "title": cand.title,
                    "category": cand.draft.category,
                    "what_happened": cand.draft.what_happened,
                    "why_it_matters": cand.draft.why_it_matters,
                    "recommended_action": cand.draft.recommended_action,
                    "how_to_execute": cand.draft.how_to_execute,
                    "entities": cand.draft.entities,
                }
                with self.ctx.tracer.step(
                    "writer", f"prose:{cand.title[:60]}", input_summary=f"{len(cl)} verified claims"
                ) as tr:
                    if state.budget_exhausted:
                        from fieldnote.agents.writer import PROSE_FIELDS, fallback_text

                        cand.prose = {f: fallback_text(f, finding, cl, budget) for f in PROSE_FIELDS}
                        cand.prose.update({"headline": cand.title, "fallback": list(PROSE_FIELDS), "regenerated": []})
                        tr.output_summary = "budget exhausted: templated prose"
                    else:
                        try:
                            cand.prose = write_finding_prose(self.ctx, finding, cl, budget)
                        except BudgetExceededError:
                            state.budget_exhausted = True
                            from fieldnote.agents.writer import PROSE_FIELDS, fallback_text

                            cand.prose = {f: fallback_text(f, finding, cl, budget) for f in PROSE_FIELDS}
                            cand.prose.update(
                                {"headline": cand.title, "fallback": list(PROSE_FIELDS), "regenerated": []}
                            )
                        tr.output_summary = f"fallback fields: {cand.prose.get('fallback')}, regenerated: {cand.prose.get('regenerated')}"
                written += 1
        return written

    def _persist(self, state: PipelineState) -> int:
        cfg = self.ctx.cfg
        now = self.ctx.now
        with self.ctx.db.session() as s:
            if self.ctx.run_id is not None:
                s.execute(
                    delete(Finding).where(Finding.workspace == cfg.workspace, Finding.first_run_id == self.ctx.run_id)
                )
            existing = repo.findings(s, cfg.workspace, statuses=("active", "demoted", "stale"))
            for cand in state.candidates:
                src_ids = sorted({x.document_id for x in cand.sources})
                match = _match_existing(cand, src_ids, existing)
                prose = cand.prose or {}
                fields: dict[str, Any] = {
                    "title": cand.title,
                    "category": cand.draft.category,
                    "status": cand.status,
                    "what_happened": prose.get("what_happened") or cand.draft.what_happened,
                    "why_it_matters": prose.get("why_it_matters") or cand.draft.why_it_matters,
                    "recommended_action": prose.get("recommended_action") or cand.draft.recommended_action,
                    "how_to_execute": prose.get("how_to_execute") or cand.draft.how_to_execute,
                    "evidence_claim_ids": cand.evidence_ids,
                    "analysis_claim_ids": cand.analysis_ids,
                    "raw_ratings": cand.raw_ratings(),
                    "scores": {
                        **cand.scores,
                        "weights": dict(cfg.scoring.weights),
                        "evidence_breakdown": cand.evidence_breakdown,
                    },
                    "total_score": cand.total,
                    "evidence_count": len(cand.evidence_ids),
                    "entities": cand.draft.entities,
                    "checks": {**cand.checks, "source_ids": src_ids},
                    "prose": prose,
                    "run_id": self.ctx.run_id,
                    "last_seen": now,
                }
                if match is not None:
                    for k, v in fields.items():
                        setattr(match, k, v)
                    runs_seen = list((match.checks or {}).get("runs_seen", []))
                    match.checks = {**fields["checks"], "runs_seen": sorted({*runs_seen, self.ctx.run_id or 0})}
                    cand.finding_id = match.id
                else:
                    row = Finding(workspace=cfg.workspace, first_run_id=self.ctx.run_id, first_seen=now, **fields)
                    row.checks = {**fields["checks"], "runs_seen": [self.ctx.run_id or 0]}
                    s.add(row)
                    s.flush()
                    existing.append(row)
                    cand.finding_id = row.id
                state.finding_ids.append(cand.finding_id)
            state.stats["stale_marked"] = repo.mark_stale_findings(s, cfg.workspace, now, cfg.scoring.stale_after_days)
        return len(state.finding_ids)


def _dedupe_candidates(cands: list[Candidate]) -> list[Candidate]:
    out: list[Candidate] = []
    for c in sorted(cands, key=lambda c: (-c.total, c.title)):
        dup = next(
            (
                o
                for o in out
                if o.draft.category == c.draft.category
                and set(o.draft.entities) == set(c.draft.entities)
                and fuzz.token_sort_ratio(o.title.lower(), c.title.lower()) >= 90
            ),
            None,
        )
        if dup is None:
            out.append(c)
            continue
        dup.evidence_ids = list(dict.fromkeys([*dup.evidence_ids, *c.evidence_ids]))
        dup.analysis_ids = list(dict.fromkeys([*dup.analysis_ids, *c.analysis_ids]))
        dup.sources = list({(s.document_id): s for s in [*dup.sources, *c.sources]}.values())
    return out


def _match_existing(cand: Candidate, src_ids: list[int], existing: list[Finding]) -> Finding | None:
    best: tuple[float, Finding] | None = None
    for f in existing:
        if f.category != cand.draft.category:
            continue
        t = fuzz.token_sort_ratio(f.title.lower(), cand.title.lower())
        old = set((f.checks or {}).get("source_ids", []))
        new = set(src_ids)
        jacc = len(old & new) / len(old | new) if (old | new) else 0.0
        same_entities = set(f.entities or []) == set(cand.draft.entities)
        if not same_entities:
            continue
        if t >= 92 or (t >= 80 and jacc >= 0.2):
            key = t + 10 * jacc
            if best is None or key > best[0]:
                best = (key, f)
    return best[1] if best else None
