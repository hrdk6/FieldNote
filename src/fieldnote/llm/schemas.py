"""Pydantic schemas for every structured LLM output (enforced via forced tool use)."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ClaimType = Literal["fact", "analysis", "estimate"]
EntailmentLabel = Literal["supported", "partially_supported", "unsupported", "contradicted"]
Intent = Literal["market_change", "competitor", "customer_voice", "metrics", "opportunities", "coordination", "general"]


# Models sometimes copy the <untrusted_content> delimiters into quotes; they must never reach a brief.
_DELIMITER_TAG = re.compile(r"</?\s*untrusted_content\s*>", re.I)


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")

    @field_validator("*", mode="before")
    @classmethod
    def _strip_delimiters(cls, value: Any) -> Any:
        if isinstance(value, str) and "untrusted_content" in value.lower():
            return _DELIMITER_TAG.sub("", value).strip()
        return value


# ---------------------------------------------------------------------------------------------
# Claims (the backbone of trust)
# ---------------------------------------------------------------------------------------------


class Derivation(_Base):
    """How an estimate (or a number in a fact) was computed. Verified in code by the critic."""

    method: str = Field(description="Plain-language description of the computation")
    op: Literal["pct_change", "difference", "ratio", "share", "sum", "median", "count", "reference", "none"] = "none"
    operands: list[float] = Field(default_factory=list)
    result: float | None = None
    reference: dict[str, str] | None = Field(
        default=None,
        description="Pointer to FieldNote-computed data, e.g. {'table': 'claim_vs_reported', 'metric': 'range', 'entity': 'X'}",
    )


class ClaimDraft(_Base):
    ref: str = Field(description="Short temporary id such as c1, c2")
    type: Literal["fact", "estimate"]
    text: str = Field(max_length=600)
    source_document_ids: list[int] = Field(default_factory=list)
    excerpt: str = Field(default="", max_length=1200, description="Short verbatim quote from the first source")
    entities: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    aspect: str | None = None
    derivation: Derivation | None = None

    @field_validator("text")
    @classmethod
    def _text_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("claim text must not be empty")
        return v.strip()


class Disagreement(_Base):
    description: str
    claim_refs: list[str] = Field(default_factory=list)
    source_document_ids: list[int] = Field(default_factory=list)


class ResearchOutput(_Base):
    claims: list[ClaimDraft] = Field(default_factory=list)
    disagreements: list[Disagreement] = Field(default_factory=list)
    notes: str = ""


# ---------------------------------------------------------------------------------------------
# Analyst
# ---------------------------------------------------------------------------------------------


class Rating(_Base):
    value: int = Field(ge=1, le=5)
    rationale: str = Field(max_length=400)


class AnalysisClaimDraft(_Base):
    text: str = Field(max_length=500)
    depends_on: list[int] = Field(min_length=1, description="Ids of verified fact/estimate claims this rests on")


class FindingDraft(_Base):
    title: str = Field(max_length=160)
    category: Literal["opportunity", "risk"]
    what_happened: str = Field(max_length=700)
    why_it_matters: str = Field(max_length=700)
    evidence_claim_ids: list[int] = Field(min_length=1)
    analysis_claims: list[AnalysisClaimDraft] = Field(default_factory=list)
    recommended_action: str = Field(max_length=500)
    how_to_execute: str = Field(max_length=700, description="Owners as roles, sequence, and timeframe")
    market_size: Rating
    competitor_gap: Rating
    effort: Rating = Field(description="1 = very low effort, 5 = very high effort")
    entities: list[str] = Field(default_factory=list)


class AnalystOutput(_Base):
    findings: list[FindingDraft] = Field(default_factory=list)


# ---------------------------------------------------------------------------------------------
# Critic
# ---------------------------------------------------------------------------------------------


class EntailmentJudgement(_Base):
    claim_id: int
    label: EntailmentLabel
    reason: str = Field(max_length=300)


class EntailmentOutput(_Base):
    judgements: list[EntailmentJudgement] = Field(default_factory=list)


class CounterEvidenceJudgement(_Base):
    document_id: int
    verdict: Literal["contradicts", "supports", "unrelated"]
    note: str = Field(default="", max_length=300)


class CounterEvidenceOutput(_Base):
    judgements: list[CounterEvidenceJudgement] = Field(default_factory=list)


# ---------------------------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------------------------


class ProseOutput(_Base):
    headline: str = Field(default="", max_length=160)
    what_happened: str = Field(max_length=600)
    why_it_matters: str = Field(max_length=600)
    recommended_action: str = Field(max_length=400)
    how_to_execute: str = Field(max_length=500)


class AnswerSentence(_Base):
    text: str = Field(max_length=500)
    claim_ids: list[int] = Field(default_factory=list)


class AnswerOutput(_Base):
    sentences: list[AnswerSentence] = Field(default_factory=list)
    missing: str = Field(default="", description="What evidence is missing, if the answer is incomplete")


# ---------------------------------------------------------------------------------------------
# Processing (fast model)
# ---------------------------------------------------------------------------------------------


class VoiceTagItem(_Base):
    document_id: int
    entities: list[str] = Field(default_factory=list)
    aspect: str = "other"
    sentiment: float = Field(ge=-1.0, le=1.0)
    quote: str = Field(default="", max_length=400)
    language: str = "en"


class VoiceTagOutput(_Base):
    items: list[VoiceTagItem] = Field(default_factory=list)


class ThemeLabel(_Base):
    cluster_id: int
    label: str = Field(max_length=80)


class ThemeLabelOutput(_Base):
    labels: list[ThemeLabel] = Field(default_factory=list)


class EntityResolutionItem(_Base):
    document_id: int
    entities: list[str] = Field(default_factory=list)


class EntityResolutionOutput(_Base):
    items: list[EntityResolutionItem] = Field(default_factory=list)


class ChangeClassificationItem(_Base):
    index: int
    kind: Literal["price", "feature", "launch", "policy", "dealer", "other"]
    confidence: float = Field(ge=0, le=1, default=0.5)


class ChangeClassificationOutput(_Base):
    items: list[ChangeClassificationItem] = Field(default_factory=list)


class ReportedValue(_Base):
    document_id: int
    value: float | None = None
    unit: str = ""
    first_hand: bool = True
    conditions: dict[str, str] = Field(default_factory=dict)
    excerpt: str = Field(default="", max_length=400)


class ReportedValuesOutput(_Base):
    items: list[ReportedValue] = Field(default_factory=list)


class ClaimedValue(_Base):
    document_id: int
    entity: str
    value: float
    unit: str = ""
    excerpt: str = Field(default="", max_length=400)


class ClaimedValuesOutput(_Base):
    items: list[ClaimedValue] = Field(default_factory=list)


class RouterOutput(_Base):
    intent: Intent
    entities: list[str] = Field(default_factory=list)
    needs_research: bool = False
    search_query: str = ""


class ActionItemDraft(_Base):
    description: str = Field(max_length=400)
    owner: str | None = None
    due_text: str | None = Field(default=None, description="The due-date phrase exactly as written, e.g. 'by Friday'")
    priority: Literal["low", "medium", "high"] = "medium"
    confidence: float = Field(ge=0, le=1, default=0.7)
    source_sentence: str = Field(max_length=600)


class ActionItemsOutput(_Base):
    items: list[ActionItemDraft] = Field(default_factory=list)


# ---------------------------------------------------------------------------------------------
# Workspace builder (plain-language topic -> draft workspace). Every URL is verified before use.
# ---------------------------------------------------------------------------------------------


class DraftPage(_Base):
    url: str = Field(description="Exact URL of an official pricing, product, dealer or newsroom page")
    kind: Literal["pricing", "product", "news", "dealers"] = "product"


class DraftEntity(_Base):
    name: str = Field(description="Canonical name of a company, brand, product line or technology to track")
    aliases: list[str] = Field(
        default_factory=list, description="Other names people use in headlines and posts (no generic words)"
    )
    kind: Literal["company", "brand", "product", "technology", "organization", "other"] = "company"
    website: str = Field(default="", description="Official homepage URL, only if you are confident it exists")
    pages: list[DraftPage] = Field(default_factory=list)


class DraftAspect(_Base):
    name: str = Field(description="snake_case aspect customers talk about, e.g. price, battery_life, delivery_speed")
    keywords: list[str] = Field(default_factory=list, description="Words that signal this aspect in reviews/posts")


class DraftMetric(_Base):
    metric: str = Field(description="snake_case numeric spec that brands claim and users report, e.g. range")
    unit: str = Field(description="Unit as written, e.g. km, hours, minutes")
    unit_aliases: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    plausible_min: float | None = None
    plausible_max: float | None = None


class WorkspaceDraft(_Base):
    display_name: str = Field(description="Short title, e.g. 'Quick commerce, India'")
    sector: str = Field(description="What is tracked, e.g. 'quick-commerce grocery delivery apps'")
    region: str = Field(description="Country or 'Global'")
    timezone: str = Field(default="UTC", description="IANA timezone for the region, e.g. Asia/Kolkata")
    language_hints: list[str] = Field(default_factory=lambda: ["en"], description="ISO 639-1 codes, e.g. ['en']")
    perspective: str = Field(default="market-level observer", description="Viewpoint briefs are written from")
    focal_company: str | None = None
    entities: list[DraftEntity] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    aspects: list[DraftAspect] = Field(default_factory=list)
    news_queries: list[str] = Field(
        default_factory=list, description="2-6 short headline phrases or distinctive names (each 3+ characters)"
    )
    feed_urls: list[str] = Field(default_factory=list, description="RSS/Atom feed URLs you are confident exist")
    publication_sites: list[str] = Field(
        default_factory=list, description="Homepages of specialist publications covering this topic"
    )
    subreddits: list[str] = Field(default_factory=list, description="Existing subreddit names without r/")
    reddit_search_terms: list[str] = Field(default_factory=list)
    youtube_search_terms: list[str] = Field(default_factory=list)
    claim_vs_reported: list[DraftMetric] = Field(default_factory=list)
