"""Ask FieldNote: answer questions from the local database only, with citations.

Simple intents use one deterministic tool (changes, metrics, themes, findings, action items). Complex
or open questions run researcher -> critic -> writer over the stored corpus. Every factual sentence
cites verified claims; sentences whose numbers or names are not in their cited claims are dropped.
If evidence is insufficient, the answer states what is missing instead of guessing.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from fieldnote.agents.context import AgentContext, RunContextSummary
from fieldnote.agents.critic import Critic
from fieldnote.agents.researcher import run_researcher
from fieldnote.agents.writer import post_check
from fieldnote.ask.router import Route, route_question
from fieldnote.config import WorkspaceConfig, safe_join, workspace_out_dir
from fieldnote.costs import BudgetExceededError
from fieldnote.db import repo
from fieldnote.db.engine import Database, utcnow
from fieldnote.db.models import Claim
from fieldnote.llm.base import LLMClient, LLMError
from fieldnote.llm.schemas import AnswerOutput, ResearchOutput
from fieldnote.logging_setup import get_logger
from fieldnote.processing.metrics import metric_summaries
from fieldnote.reports.common import EvidenceItem, evidence_items
from fieldnote.textutil import fmt_number, slugify, split_sentences, tokenize, truncate_words

log = get_logger(__name__)

LOOKBACK_DAYS = 30
MAX_TURNS = 6


# ---------------------------------------------------------------------------------------------
# Session memory
# ---------------------------------------------------------------------------------------------


@dataclass
class AskTurn:
    question: str
    answer: str
    intent: str
    entities: list[str]
    claim_ids: list[int]


@dataclass
class AskSession:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    turns: list[AskTurn] = field(default_factory=list)

    def last_entities(self) -> list[str]:
        for t in reversed(self.turns):
            if t.entities:
                return t.entities
        return []

    def add(self, turn: AskTurn) -> None:
        self.turns.append(turn)
        self.turns = self.turns[-MAX_TURNS:]

    def save(self, workspace: str) -> None:
        folder = workspace_out_dir(workspace) / "ask_sessions"
        folder.mkdir(parents=True, exist_ok=True)
        path = safe_join(folder, f"{slugify(self.id, 40)}.json")
        path.write_text(
            json.dumps({"id": self.id, "turns": [asdict(t) for t in self.turns]}, indent=1), encoding="utf-8"
        )

    @classmethod
    def load(cls, workspace: str, session_id: str) -> AskSession:
        folder = workspace_out_dir(workspace) / "ask_sessions"
        path = safe_join(folder, f"{slugify(session_id, 40)}.json")
        if not path.exists():
            return cls(id=slugify(session_id, 40))
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(id=data["id"], turns=[AskTurn(**t) for t in data.get("turns", [])])


@dataclass
class AnswerSentence:
    text: str
    citations: list[int]


@dataclass
class Answer:
    question: str
    intent: str
    route_via: str
    sentences: list[AnswerSentence] = field(default_factory=list)
    sources: list[EvidenceItem] = field(default_factory=list)
    missing: str = ""
    internal_notes: list[str] = field(default_factory=list)
    claim_ids: list[int] = field(default_factory=list)
    markdown: str = ""
    path: str | None = None
    entities: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------------------------
# Deterministic claim builders for single-tool intents
# ---------------------------------------------------------------------------------------------


def _claims_from_changes(ctx: AgentContext, entities: list[str]) -> tuple[list[dict[str, Any]], str]:
    with ctx.db.session() as s:
        rows = repo.changes_between(s, ctx.cfg.workspace, ctx.now - timedelta(days=14), ctx.now)
        rows = [c for c in rows if not entities or c.competitor in entities][:8]
        out = [
            {
                "type": "fact",
                "text": c.summary,
                "source_document_ids": [c.document_id],
                "excerpt": c.summary,
                "entities": [c.competitor],
                "tags": ["change", c.kind],
                "derivation": {},
            }
            for c in rows
            if c.document_id
        ]
    who = f" for {', '.join(entities)}" if entities else ""
    return out, ("" if out else f"No competitor page changes were detected in the last 14 days{who}.")


def _claims_from_metrics(ctx: AgentContext, entities: list[str]) -> tuple[list[dict[str, Any]], str]:
    with ctx.db.session() as s:
        summaries = metric_summaries(s, ctx.cfg)
    if not summaries:
        return [], "No metrics connector is configured or no metric data has been loaded for this workspace."
    out: list[dict[str, Any]] = []
    for summ in summaries:
        if not summ.dataset_document_id:
            continue
        ents = [e for e in summ.entities if not entities or e.entity in entities] or summ.entities[:3]
        for e in ents[:4]:
            if e.pct_change is not None and e.previous:
                pct = round(e.pct_change, 1)
                verb = "rose" if pct > 0 else "fell"
                out.append(
                    {
                        "type": "estimate",
                        "text": (
                            f"Estimate: {e.entity} {summ.label} across tracked regions {verb} {abs(pct):.1f}% month over "
                            f"month in {summ.latest_period} ({fmt_number(e.latest)} vs {fmt_number(e.previous)} {summ.unit_short})."
                        ),
                        "source_document_ids": [summ.dataset_document_id],
                        "excerpt": e.line,
                        "entities": [e.entity],
                        "tags": ["metric"],
                        "derivation": {
                            "method": "percent change of summed monthly values",
                            "op": "pct_change",
                            "operands": [e.previous, e.latest],
                            "result": pct,
                        },
                    }
                )
            if summ.total_latest:
                share = round(e.latest / summ.total_latest * 100, 1)
                out.append(
                    {
                        "type": "estimate",
                        "text": (
                            f"Estimate: {e.entity} accounted for {share:.1f}% of tracked {summ.label} in {summ.latest_period} "
                            f"({fmt_number(e.latest)} of {fmt_number(summ.total_latest)} {summ.unit_short})."
                        ),
                        "source_document_ids": [summ.dataset_document_id],
                        "excerpt": e.line,
                        "entities": [e.entity],
                        "tags": ["metric", "share"],
                        "derivation": {
                            "method": "entity value divided by the total",
                            "op": "share",
                            "operands": [e.latest, summ.total_latest],
                            "result": share,
                        },
                    }
                )
    return out, (
        "" if out else "Metric data exists but no dataset summary document is available yet; run the pipeline once."
    )


GENERIC_QUESTION_WORDS = set(
    [
        "what",
        "which",
        "who",
        "whom",
        "how",
        "why",
        "when",
        "where",
        "tell",
        "show",
        "give",
        "list",
        "latest",
        "recent",
        "recently",
        "new",
        "top",
        "biggest",
        "most",
        "main",
        "key",
        "change",
        "changes",
        "changed",
        "changing",
        "price",
        "prices",
        "pricing",
        "priced",
        "cost",
        "costs",
        "share",
        "shares",
        "market",
        "markets",
        "sales",
        "brand",
        "brands",
        "competitor",
        "competitors",
        "company",
        "companies",
        "customers",
        "customer",
        "people",
        "users",
        "owners",
        "complaining",
        "complain",
        "complaints",
        "saying",
        "say",
        "said",
        "think",
        "feel",
        "like",
        "trend",
        "trends",
        "gained",
        "gain",
        "gaining",
        "lost",
        "lose",
        "losing",
        "grew",
        "grow",
        "growth",
        "month",
        "months",
        "week",
        "weeks",
        "year",
        "years",
        "quarter",
        "today",
        "yesterday",
        "last",
        "this",
        "that",
        "our",
        "us",
        "we",
        "should",
        "respond",
        "response",
        "do",
        "does",
        "did",
        "action",
        "actions",
        "items",
        "item",
        "open",
        "due",
        "overdue",
        "opportunities",
        "opportunity",
        "risks",
        "risk",
        "findings",
        "finding",
        "doing",
        "happening",
        "happen",
        "happened",
        "about",
        "going",
        "compare",
        "comparison",
        "versus",
        "vs",
        "between",
        "data",
        "numbers",
        "number",
        "metrics",
        "metric",
        "big",
        "small",
        "good",
        "bad",
        "better",
        "worse",
        "best",
        "worst",
        "anything",
        "something",
        "everything",
        "other",
        "others",
        "any",
        "all",
        "there",
        "their",
        "them",
        "they",
        "it",
        "its",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "can",
        "could",
        "would",
        "will",
        "pricing",
        "launches",
        "launched",
        "launch",
        "policy",
        "policies",
        "dealers",
        "dealer",
        "stores",
        "update",
        "updates",
        "complaining",
        "talking",
        "discuss",
        "discussed",
        "mention",
        "mentioned",
        "summary",
        "summarise",
        "summarize",
        "explain",
        "overview",
        "status",
        "report",
    ]
)


def out_of_scope_terms(cfg: WorkspaceConfig, question: str, entities: list[str]) -> list[str]:
    """Question terms unrelated to anything this workspace tracks (used to say what is missing, not guess)."""
    if entities:
        return []
    vocab_text = " ".join(
        [
            cfg.sector,
            cfg.display_name,
            cfg.region,
            cfg.perspective,
            *cfg.keywords,
            *cfg.aspects,
            *[t for a in cfg.aspects for t in cfg.aspect_terms(a)],
            *cfg.entity_aliases(),
            *[str((c.model_extra or {}).get("label") or c.name or "") for c in cfg.connectors],
            *[c.metric for c in cfg.claim_vs_reported],
            *[k for c in cfg.claim_vs_reported for k in c.all_keywords()],
        ]
    )
    vocab = set(tokenize(vocab_text, keep_numbers=False)) | {
        w.rstrip("s") for w in tokenize(vocab_text, keep_numbers=False)
    }
    terms = [t for t in tokenize(question, keep_numbers=False) if t not in GENERIC_QUESTION_WORDS]
    if not terms:
        return []
    if any(t in vocab or t.rstrip("s") in vocab for t in terms):
        return []
    return terms


def _question_aspects(cfg: WorkspaceConfig, question: str) -> set[str]:
    q = question.lower()
    return {a for a in cfg.aspects if any(term in q for term in cfg.aspect_terms(a))}


def _claims_from_voice(ctx: AgentContext, entities: list[str], question: str = "") -> tuple[list[dict[str, Any]], str]:
    from fieldnote.db.models import ClaimVsReported, Theme

    out: list[dict[str, Any]] = []
    aspects = _question_aspects(ctx.cfg, question)
    with ctx.db.session() as s:
        run = repo.latest_run_with(s, ctx.cfg.workspace, Theme)
        themes = repo.themes_for_run(s, ctx.cfg.workspace, run) if run else []

        def relevance(t: Theme) -> float:
            ents = t.entity_distribution or {}
            asp = t.aspect_distribution or {}
            score = 0.0
            if entities:
                share = max((ents.get(e, 0) for e in entities), default=0) / max(1, t.size)
                if share < 0.3:
                    return -1.0
                score += share
            if aspects:
                top = max(asp, key=lambda k: asp[k]) if asp else None
                if top not in aspects:
                    return -1.0
                score += 1.0
            return score + t.size / 100.0

        ranked = sorted((t for t in themes if relevance(t) >= 0), key=lambda t: (-relevance(t), t.id))
        for t in ranked[: 3 if (entities or aspects) else 6]:
            docs = repo.get_documents(s, t.sample_document_ids[:3])
            first = next((docs[i] for i in t.sample_document_ids[:3] if i in docs), None)
            if first is None:
                continue
            sents = split_sentences(first.content)
            excerpt = truncate_words(sents[1] if len(sents) > 1 else (sents[0] if sents else first.content), 40, "")
            out.append(
                {
                    "type": "estimate",
                    "text": (
                        f"Estimate: {t.size} customer posts in the current window clustered under the theme "
                        f"'{t.label}', with mean sentiment {t.sentiment_mean:+.2f}."
                    ),
                    "source_document_ids": [first.id, *[i for i in t.sample_document_ids[:3] if i != first.id]],
                    "excerpt": excerpt,
                    "entities": [],
                    "tags": ["voice"],
                    "aspect": None,
                    "derivation": {
                        "method": "clustered and counted in code",
                        "op": "reference",
                        "reference": {"table": "themes", "theme_id": str(t.id)},
                    },
                }
            )
        cvr_run = repo.latest_run_with(s, ctx.cfg.workspace, ClaimVsReported)
        metric_terms = {c.metric: c.all_keywords() for c in ctx.cfg.claim_vs_reported}
        for r in repo.cvr_for_run(s, ctx.cfg.workspace, cvr_run) if cvr_run else []:
            if (
                (entities and r.entity not in entities)
                or r.reported_median is None
                or r.claimed_value is None
                or not r.reported_values
            ):
                continue
            asked_metric = any(k in question.lower() for k in metric_terms.get(r.metric, []))
            if aspects and not asked_metric:
                continue
            low = "low_confidence" in (r.flags or [])
            metric = r.metric.replace("_", " ")
            med, q1, q3 = (
                fmt_number(v, 1 if v % 1 else 0) for v in (r.reported_median, r.reported_q1 or 0, r.reported_q3 or 0)
            )
            claimed = fmt_number(r.claimed_value, 1 if r.claimed_value % 1 else 0)
            prefix = f"Estimate (low confidence, n={r.n})" if low else "Estimate"
            n_part = "" if low else f", n={r.n}"
            sample = r.reported_values[0]
            out.append(
                {
                    "type": "estimate",
                    "text": f"{prefix}: owner-reported {metric} for {r.entity} has a median of {med} {r.unit} (IQR {q1}-{q3}{n_part}) against a claimed {claimed} {r.unit}.",
                    "source_document_ids": [sample["document_id"]],
                    "excerpt": sample.get("excerpt", ""),
                    "entities": [r.entity],
                    "tags": ["claim_vs_reported"],
                    "aspect": r.metric,
                    "derivation": {
                        "method": "median/IQR of first-hand reports",
                        "op": "reference",
                        "reference": {"table": "claim_vs_reported", "metric": r.metric, "entity": r.entity},
                    },
                }
            )
    who = f" mentioning {', '.join(entities)}" if entities else ""
    return out, ("" if out else f"No customer-voice themes or owner reports{who} are stored yet.")


# ---------------------------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------------------------


def _persist_and_verify(ctx: AgentContext, drafts: list[dict[str, Any]]) -> list[int]:
    if not drafts:
        return []
    critic = Critic(ctx)
    with ctx.db.session() as s:
        rows = []
        for d in drafts:
            row = Claim(
                workspace=ctx.cfg.workspace,
                run_id=None,
                agent="ask",
                type=d["type"],
                text=d["text"],
                source_document_ids=[int(x) for x in d.get("source_document_ids", []) if x],
                excerpt=d.get("excerpt", ""),
                entities=d.get("entities", []),
                tags=d.get("tags", []),
                aspect=d.get("aspect"),
                derivation=d.get("derivation") or {},
                status="unverified",
            )
            s.add(row)
            rows.append(row)
        s.flush()
        try:
            critic.verify_claims(s, rows)
        except BudgetExceededError:
            log.warning("LLM budget reached while verifying answer claims")
        return [r.id for r in rows if r.status == "verified"]


def _research_claims(ctx: AgentContext, route: Route, question: str) -> list[dict[str, Any]]:
    summary = RunContextSummary(queries=[route.search_query, *route.entities][:4])
    out: ResearchOutput = run_researcher(ctx, summary, focus=question, max_claims=10)
    return [c.model_dump() for c in out.claims]


def _compose(ctx: AgentContext, question: str, verified: list[Claim]) -> list[AnswerSentence]:
    claims_payload = [{"id": c.id, "type": c.type, "text": c.text} for c in verified]
    order = {c.id: i + 1 for i, c in enumerate(verified)}
    sentences: list[AnswerSentence] = []
    try:
        out = ctx.llm.structured(
            task="ask_answer",
            system=(
                "You are FieldNote. Answer the question using ONLY the verified claims given. Write short sentences; each "
                "sentence lists the claim ids it relies on. Do not add numbers or names not present in those claims. "
                "Keep estimates labelled as estimates."
            ),
            prompt=f"Question: {question}\n\nVerified claims:\n"
            + "\n".join(f"[{c['id']}] ({c['type']}) {c['text']}" for c in claims_payload),
            schema=AnswerOutput,
            tier="main",
            payload={"question": question, "claims": claims_payload},
        )
        by_id = {c.id: c for c in verified}
        for snt in out.sentences:
            cited = [i for i in snt.claim_ids if i in by_id]
            if not cited:
                continue
            if not post_check(snt.text, [by_id[i].text for i in cited], ctx.cfg).ok:
                continue
            sentences.append(AnswerSentence(text=snt.text, citations=sorted({order[i] for i in cited})))
    except (LLMError, BudgetExceededError) as exc:
        log.info("answer composition fell back to claim text: %s", exc)
    if not sentences:
        sentences = [AnswerSentence(text=c.text, citations=[order[c.id]]) for c in verified[:6]]
    return sentences


def _coordination_answer(db: Database, cfg: WorkspaceConfig, entities: list[str]) -> list[str]:
    with db.session() as s:
        items = repo.open_action_items(s, cfg.workspace)
    if not items:
        return ["There are no open action items in the tracker."]
    lines = []
    for a in items[:10]:
        due = a.due_date.isoformat() if a.due_date else "no due date"
        lines.append(f"Action #{a.id}: {a.description} (owner: {a.owner}; due {due}; priority {a.priority}).")
    return lines


def render_markdown(ans: Answer) -> str:
    lines = [f"**Question:** {ans.question}", ""]
    if ans.internal_notes:
        lines += [f"- {n}" for n in ans.internal_notes]
        lines.append("")
        lines.append("_Source: FieldNote action tracker (internal data)._")
    for snt in ans.sentences:
        cites = "".join(f"[{n}]" for n in snt.citations)
        lines.append(f"{snt.text} {cites}".strip())
    if ans.missing:
        lines += ["", f"_Not enough evidence: {ans.missing}_"]
    if ans.sources:
        lines += ["", "**Sources**"]
        for e in ans.sources:
            q = f' "{e.quote}"' if e.show_quote else ""
            lines.append(f"{e.n}. [{e.label}] {e.text}{q} ([{e.source_name or 'source'}]({e.url}), {e.published})")
    return "\n".join(lines)


def ask(
    db: Database,
    cfg: WorkspaceConfig,
    question: str,
    *,
    llm: LLMClient,
    session: AskSession | None = None,
    now: datetime | None = None,
    record: bool = True,
) -> Answer:
    question = question.strip()
    now = now or utcnow()
    ctx = AgentContext.build(cfg, db, llm, None, now, now - timedelta(days=LOOKBACK_DAYS))
    route = route_question(cfg, llm, question, session.last_entities() if session else None)
    ans = Answer(question=question, intent=route.intent, route_via=route.via, entities=route.entities)
    missing = ""
    verified_ids: list[int] = []
    unknown = out_of_scope_terms(cfg, question, route.entities) if route.intent != "coordination" else []
    try:
        if unknown:
            missing = (
                f"This workspace tracks {cfg.sector} in {cfg.region}; nothing stored relates to "
                f"{', '.join(repr(t) for t in unknown[:4])}. Add those sources or keywords to the workspace to cover it."
            )
        elif route.intent == "coordination":
            ans.internal_notes = _coordination_answer(db, cfg, route.entities)
        elif route.intent == "opportunities":
            with db.session() as s:
                rows = repo.findings(s, cfg.workspace, statuses=("active", "demoted"))[:5]
                verified_ids = [cid for f in rows for cid in (f.evidence_claim_ids or [])[:2]]
                claims = repo.get_claims(s, verified_ids)
                verified_ids = [i for i in verified_ids if i in claims and claims[i].status == "verified"]
            if not verified_ids:
                missing = "There are no verified findings on the Opportunity Board yet; run the pipeline first."
        else:
            if route.intent == "market_change" and not route.needs_research:
                drafts, missing = _claims_from_changes(ctx, route.entities)
            elif route.intent == "metrics" and not route.needs_research:
                drafts, missing = _claims_from_metrics(ctx, route.entities)
            elif route.intent == "customer_voice" and not route.needs_research:
                drafts, missing = _claims_from_voice(ctx, route.entities, question)
            else:
                with ctx.tracer.step("ask", "research", input_summary=question[:200]):
                    drafts = _research_claims(ctx, route, question)
                if not drafts:
                    who = f" about {', '.join(route.entities)}" if route.entities else ""
                    missing = (
                        f"No stored documents from the last {LOOKBACK_DAYS} days{who} contain sourced statements that "
                        "answer this question."
                    )
            verified_ids = _persist_and_verify(ctx, drafts)
            if drafts and not verified_ids:
                missing = "Candidate statements were found but none passed verification against their sources."
    except (LLMError, BudgetExceededError) as exc:
        missing = f"The answer could not be completed: {exc}"
    if verified_ids:
        with db.session() as s:
            claims_map = repo.get_claims(s, verified_ids)
            verified = [claims_map[i] for i in verified_ids if i in claims_map]
            ans.sources = evidence_items(s, [c.id for c in verified])
        ans.sentences = _compose(ctx, question, verified)
        ans.claim_ids = [c.id for c in verified]
    ans.missing = missing
    ans.markdown = render_markdown(ans)
    llm.cache.flush()
    ctx.tracer.flush()
    if record:
        folder = workspace_out_dir(cfg.workspace) / "ask"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{now:%Y%m%d-%H%M%S}_{slugify(question, 40)}.md"
        path.write_text(ans.markdown, encoding="utf-8")
        ans.path = str(path)
        with db.session() as s:
            repo.upsert_brief(
                s,
                cfg.workspace,
                kind="ask",
                path=str(path),
                run_id=None,
                formats={"md": str(path)},
                meta={
                    "question": question,
                    "intent": route.intent,
                    "via": route.via,
                    "claim_ids": ans.claim_ids,
                    "sources": [e.url for e in ans.sources],
                    "missing": missing,
                    "session": session.id if session else None,
                },
            )
    if session is not None:
        session.add(
            AskTurn(
                question=question,
                answer=ans.markdown[:2000],
                intent=route.intent,
                entities=route.entities,
                claim_ids=ans.claim_ids,
            )
        )
    return ans
