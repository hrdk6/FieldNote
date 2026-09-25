"""Workspace builder: plain-language topic -> verified, validated workspace YAML.

Steps:
1. Draft: the LLM proposes entities, aliases, aspects, search phrases and candidate sources
   (:class:`~fieldnote.llm.schemas.WorkspaceDraft`). Without an LLM key, or if the call fails, a
   rule-based draft is used instead (:mod:`fieldnote.builder.heuristic`), which never proposes URLs.
2. Verify: every URL is fetched politely and kept only if it works (:mod:`fieldnote.builder.verify`).
   URLs the user typed into the description are verified the same way.
3. Assemble: limits, alias de-duplication, snake_case aspects, timezone and region defaults, then the
   result is validated by the same strict :class:`~fieldnote.config.WorkspaceConfig` model as any
   hand-written file and rendered as commented YAML (dropped sources are listed at the end).
"""

from __future__ import annotations

import re
import textwrap
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import ValidationError

from fieldnote.builder.heuristic import _GENERIC_ASPECTS, heuristic_draft, region_terms, strip_region_terms
from fieldnote.builder.verify import Candidates, SourceCheck, verify_candidates
from fieldnote.collectors.base import PoliteHttpClient
from fieldnote.collectors.gdelt import gdelt_term
from fieldnote.config import (
    GDELT_LANGUAGES,
    SLUG_RE,
    ClaimVsReportedConfig,
    ConfigError,
    Settings,
    WorkspaceConfig,
    parse_workspace,
    region_profile,
)
from fieldnote.costs import BudgetExceededError
from fieldnote.llm.base import LLMClient, LLMError
from fieldnote.llm.schemas import WorkspaceDraft
from fieldnote.logging_setup import get_logger
from fieldnote.textutil import UNTRUSTED_NOTICE, canonical_url, slugify, wrap_untrusted

log = get_logger(__name__)

MAX_ENTITIES = 12
MAX_ALIASES = 6
MAX_PAGES_PER_ENTITY = 2
MAX_PAGES = 12
MAX_FEEDS = 12
MAX_SITES = 12
MAX_QUERIES = 6
MAX_ASPECTS = 8
MAX_KEYWORDS = 12
MAX_SUBREDDITS = 8
MAX_REDDIT_TERMS = 6
MAX_YOUTUBE_TERMS = 3
MIN_DESCRIPTION = 8
MAX_DESCRIPTION = 3000
WORLDWIDE = "worldwide"
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
_LANG_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$")

Progress = Callable[[str], None]

SYSTEM_PROMPT = f"""You design monitoring workspaces for FieldNote, a market-intelligence assistant that
collects public news, web pages and customer discussion about a topic and writes cited briefs.

Given a plain-language topic, return a draft workspace by calling the tool. Guidelines:
- entities: the 4-12 most relevant players for the topic (companies, brands, product lines or
  technologies). Aliases: other names used in headlines and posts (short forms, product lines);
  never generic words, never an alias shared by two entities.
- aspects: 4-8 snake_case themes customers or buyers discuss (e.g. price, battery_life,
  delivery_speed, app_experience), each with 4-8 signal words.
- news_queries: 2-6 short phrases likely to appear verbatim in headlines (1-3 words each, at least
  3 characters), including distinctive entity names. Avoid ambiguous common words, and do not add the
  country or region: news is already filtered to sources from the region.
- keywords: topic phrases that signal a relevant article (not the region name).
- perspective: whose decisions the briefs support, e.g. "new-entrant challenger brand",
  "category leader", "investor", "product team" or "market-level observer" (not the end customer).
- URLs: propose only URLs you are highly confident exist exactly as written: official homepages,
  official pricing/product/newsroom pages (at most 2 per entity) and specialist publications that
  cover this topic in this region (their RSS feed URL in feed_urls, or their homepage in
  publication_sites). Every URL is fetched and verified; unverifiable ones are dropped, so prefer
  fewer, well-known URLs.
- subreddits: only real, active communities where buyers discuss this topic.
- claim_vs_reported: only for a numeric spec that brands advertise and users commonly report
  (e.g. range in km, battery life in hours); otherwise leave it empty.
- region: the country named or implied by the topic, or "Global". timezone: its IANA name.
- Never include personal data about private individuals.

{UNTRUSTED_NOTICE}"""


class BuilderError(Exception):
    """The builder could not produce a valid workspace."""


@dataclass
class BuildRequest:
    description: str
    region: str | None = None
    perspective: str | None = None
    focal_company: str | None = None
    name: str | None = None
    language: str | None = None

    def validate(self) -> None:
        text = (self.description or "").strip()
        if len(text) < MIN_DESCRIPTION:
            raise BuilderError("Describe what to track in a few words (at least 8 characters).")
        if len(text) > MAX_DESCRIPTION:
            raise BuilderError(f"Keep the description under {MAX_DESCRIPTION} characters.")
        if self.name and not SLUG_RE.match(self.name):
            raise BuilderError("The workspace id must use lowercase letters, digits and underscores (2-63 chars).")
        if self.language and not _LANG_RE.match(self.language.strip().lower()):
            raise BuilderError("Language must be an ISO 639-1 code such as 'en' or 'hi'.")

    def payload(self) -> dict[str, Any]:
        return {
            "description": self.description.strip(),
            "region": (self.region or "").strip() or None,
            "perspective": (self.perspective or "").strip() or None,
            "focal_company": (self.focal_company or "").strip() or None,
            "language": (self.language or "").strip().lower() or None,
        }


@dataclass
class BuildResult:
    request: BuildRequest
    draft: WorkspaceDraft
    config: dict[str, Any]
    checks: list[SourceCheck]
    warnings: list[str]
    drafted_by: str
    yaml_text: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC).replace(tzinfo=None))

    @property
    def name(self) -> str:
        return str(self.config["workspace"])

    def counts(self) -> dict[str, int]:
        out = {"ok": 0, "dropped": 0, "unverified": 0}
        for c in self.checks:
            out[c.status] += 1
        return out


# ---- step 1: draft ----------------------------------------------------------------------------


def draft_workspace(req: BuildRequest, llm: LLMClient) -> tuple[WorkspaceDraft, str, list[str]]:
    payload = req.payload()
    warnings: list[str] = []
    hints = [f"{k.replace('_', ' ')}: {v}" for k, v in payload.items() if k != "description" and v]
    prompt = (
        f"Today is {datetime.now(UTC):%Y-%m-%d}.\n\nTopic to track (user input):\n"
        f"{wrap_untrusted(payload['description'])}\n\n"
        + ("User preferences:\n" + "\n".join(f"- {h}" for h in hints) + "\n\n" if hints else "")
        + "Return the draft workspace."
    )
    heuristic = WorkspaceDraft.model_validate(heuristic_draft(payload))
    drafted_by = "rule-based (no LLM key configured)" if llm.is_mock else llm.name
    try:
        draft = llm.structured(
            task="workspace_draft",
            system=SYSTEM_PROMPT,
            prompt=prompt,
            schema=WorkspaceDraft,
            tier="main",
            payload=payload,
            max_tokens=6000,
        )
    except (LLMError, BudgetExceededError) as exc:
        log.warning("LLM workspace draft failed: %s", exc)
        warnings.append(f"The {llm.name} draft failed ({str(exc)[:160]}); a rule-based draft was used instead.")
        return heuristic, "rule-based (LLM draft failed)", warnings
    if not draft.entities:
        warnings.append("The model proposed no entities; the ones named in your description were used.")
        draft.entities = heuristic.entities
    if not draft.news_queries:
        draft.news_queries = heuristic.news_queries
    if not draft.aspects:
        draft.aspects = heuristic.aspects
    return draft, drafted_by, warnings


# ---- step 2: candidates --------------------------------------------------------------------------


def urls_in(text: str) -> list[str]:
    return list(dict.fromkeys(u.rstrip(".,;:!?") for u in _URL_RE.findall(text or "")))


def _is_http(url: str) -> bool:
    return bool(re.match(r"^https?://[^\s/$.?#][^\s]*$", (url or "").strip()))


def collect_candidates(draft: WorkspaceDraft, user_urls: list[str]) -> Candidates:
    c = Candidates()
    for u in user_urls:
        if _is_http(u):
            c.sites.append((u.strip(), "user", None))
    for u in draft.feed_urls:
        if _is_http(u):
            c.feeds.append((u.strip(), "model"))
    for u in draft.publication_sites:
        if _is_http(u):
            c.sites.append((u.strip(), "model", None))
    pages = 0
    for ent in draft.entities[:MAX_ENTITIES]:
        if ent.website and _is_http(ent.website):
            c.sites.append((ent.website.strip(), "model", ent.name))
        for p in ent.pages[:MAX_PAGES_PER_ENTITY]:
            if pages < MAX_PAGES and _is_http(p.url):
                c.pages.append((p.url.strip(), "model", ent.name, p.kind))
                pages += 1
    c.feeds = c.feeds[:MAX_FEEDS]
    c.sites = c.sites[:MAX_SITES]
    for s in draft.subreddits:
        name = s.strip().removeprefix("/").removeprefix("r/").strip("/")
        if name and name.lower() not in {x.lower() for x in c.subreddits}:
            c.subreddits.append(name)
    c.subreddits = c.subreddits[:MAX_SUBREDDITS]
    return c


# ---- step 3: assemble ------------------------------------------------------------------------------


def _clean_list(items: list[str], limit: int, max_len: int = 60, min_len: int = 2) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for it in items:
        s = " ".join(str(it).split()).strip(" ,;")
        if min_len <= len(s) <= max_len and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
        if len(out) >= limit:
            break
    return out


def _snake(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    if s and not s[0].isalpha():
        s = f"a_{s}"
    return s[:40]


def unique_slug(base: str, taken: set[str]) -> str:
    slug = slugify(base, max_len=50).replace("-", "_")
    slug = re.sub(r"_+", "_", slug).strip("_")
    if not slug or not slug[0].isalnum():
        slug = f"ws_{slug}".strip("_")
    if len(slug) < 2:
        slug = "workspace"
    candidate, n = slug, 2
    while candidate in taken:
        candidate = f"{slug}_{n}"
        n += 1
    return candidate


def _timezone(value: str, region: str) -> str:
    for cand in (value, (region_profile(region) or ("", ""))[1], "UTC"):
        if not cand:
            continue
        try:
            ZoneInfo(cand)
            return cand
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return "UTC"


def assemble_config(
    req: BuildRequest,
    draft: WorkspaceDraft,
    checks: list[SourceCheck],
    *,
    search_country: str | None,
    taken: set[str],
    warnings: list[str],
) -> dict[str, Any]:
    payload = req.payload()
    region = " ".join((payload["region"] or draft.region or "Global").split())[:60] or "Global"
    display = " ".join(draft.display_name.split())[:80] or region
    sector = " ".join(draft.sector.split())[:120] or display.lower()
    slug = req.name if req.name else unique_slug(display, taken)
    if req.name and req.name in taken:
        raise BuilderError(f"A workspace named '{req.name}' already exists; choose another id.")

    # Entities: unique names; aliases never shared across entities.
    claimed: dict[str, str] = {}
    competitors: list[dict[str, Any]] = []
    pages_by_entity: dict[str, list[dict[str, Any]]] = {}
    for chk in checks:
        if chk.kind == "page" and chk.status == "ok" and chk.entity:
            pages_by_entity.setdefault(chk.entity, []).append(
                {"url": chk.value, "kind": chk.page_kind or "product", "render": False, "selector": None}
            )
    for ent in draft.entities:
        name = " ".join(ent.name.split())[:60]
        if len(name) < 2 or name.lower() in claimed:
            continue
        claimed[name.lower()] = name
        aliases = []
        for a in ent.aliases:
            a = " ".join(str(a).split())[:60]
            if len(a) >= 2 and a.lower() not in claimed:
                claimed[a.lower()] = name
                aliases.append(a)
            if len(aliases) >= MAX_ALIASES:
                break
        competitors.append({"name": name, "aliases": aliases, "pages": pages_by_entity.get(ent.name, [])})
        if len(competitors) >= MAX_ENTITIES:
            break
    if not competitors:
        raise BuilderError("No companies, brands or technologies could be identified. Name a few in the description.")

    # Aspects: snake_case, unique, 2-8 of them.
    aspects: list[str] = []
    aspect_keywords: dict[str, list[str]] = {}
    for asp in draft.aspects:
        key = _snake(asp.name)
        if not re.match(r"^[a-z][a-z0-9_]{0,40}$", key) or key == "other" or key in aspects:
            continue
        aspects.append(key)
        kws = _clean_list([k.lower() for k in asp.keywords], 12, max_len=40)
        if kws:
            aspect_keywords[key] = kws
        if len(aspects) >= MAX_ASPECTS:
            break
    for key, kws in _GENERIC_ASPECTS.items():
        if len(aspects) >= 3:
            break
        if key not in aspects:
            aspects.append(key)
            aspect_keywords[key] = kws

    # Sources.
    feeds: list[str] = []
    seen_feeds: set[str] = set()
    for chk in checks:
        if chk.status == "ok" and chk.feed_url and chk.kind in ("feed", "site"):
            key = canonical_url(chk.feed_url)
            if key not in seen_feeds:
                seen_feeds.add(key)
                feeds.append(chk.feed_url)
    feeds = feeds[:MAX_FEEDS]
    # Multi-word search terms are exact phrases in GDELT and results are already filtered to the region's
    # sources, so "Blinkit India" -> "Blinkit" (names such as "Bank of India" are left alone).
    rterms = region_terms(region)
    protected = {n.lower() for c in competitors for n in [c["name"], *c["aliases"]]}
    stripped = [strip_region_terms(q.replace('"', " "), rterms, protected) for q in draft.news_queries]
    queries = [q for q in _clean_list(stripped, MAX_QUERIES, max_len=80, min_len=3) if gdelt_term(q)]
    news_check = next((c for c in checks if c.kind == "news_search"), None)
    if news_check is not None and news_check.status == "dropped":
        warnings.append(f"News search disabled: {news_check.detail}.")
        queries = []
    if not feeds and not queries:
        warnings.append("No verified news source yet: add RSS feeds or search phrases before the first run.")
    subreddits = [c.value for c in checks if c.kind == "subreddit" and c.status in ("ok", "unverified")]
    languages = [h.strip().lower() for h in ([payload["language"]] if payload["language"] else draft.language_hints)]
    languages = [h for h in dict.fromkeys(languages) if _LANG_RE.match(h)] or ["en"]
    lang_name = next((GDELT_LANGUAGES[h] for h in languages if h in GDELT_LANGUAGES), None)
    country = search_country if search_country else (WORLDWIDE if region_profile(region) else None)

    metrics: list[dict[str, Any]] = []
    for m in draft.claim_vs_reported[:2]:
        entry: dict[str, Any] = {
            "metric": _snake(m.metric),
            "unit": " ".join(m.unit.split())[:20],
            "unit_aliases": _clean_list(m.unit_aliases, 5, max_len=20, min_len=1),
            "keywords": _clean_list([k.lower() for k in m.keywords], 8, max_len=40),
            "claimed_extract": "regex",
            "reported_extract": "llm",
            "min_n": 8,
        }
        if m.plausible_min is not None and m.plausible_max is not None and m.plausible_min < m.plausible_max:
            entry["plausible_range"] = [float(m.plausible_min), float(m.plausible_max)]
        try:
            ClaimVsReportedConfig.model_validate(entry)
        except ValidationError:
            log.info("dropping invalid claim-vs-reported metric %r from the draft", entry.get("metric"))
            continue
        if entry["metric"] and entry["unit"] and entry["metric"] not in {x["metric"] for x in metrics}:
            metrics.append(entry)

    # Keywords also decide whether a news hit is relevant; a bare region word ("India") would match everything.
    keywords = [k for k in _clean_list([*draft.keywords, sector], MAX_KEYWORDS + 4) if k.lower() not in rterms]
    keywords = keywords[:MAX_KEYWORDS]
    focal = " ".join((payload["focal_company"] or draft.focal_company or "").split())[:80] or None
    perspective = " ".join((payload["perspective"] or draft.perspective or "market-level observer").split())[:200]
    return {
        "workspace": slug,
        "display_name": display,
        "sector": sector,
        "region": region,
        "timezone": _timezone(draft.timezone, region),
        "language_hints": languages,
        "focal_company": focal,
        "perspective": perspective,
        "competitors": competitors,
        "keywords": keywords,
        "aspects": aspects,
        "aspect_keywords": aspect_keywords,
        "sources": {
            "news": {
                "search_queries": queries,
                "search_country": country,
                "search_language": lang_name,
                "rss_feeds": feeds,
                # Builder-chosen feeds are often broad publications: keep only items about this topic.
                "filter_feeds_by_relevance": True,
                "google_news_queries": [],
                "lookback_days": 7,
                "max_items_per_feed": 25,
                "fetch_full_text": True,
            },
            "reddit": {
                "subreddits": subreddits,
                "search_terms": _clean_list(draft.reddit_search_terms, MAX_REDDIT_TERMS),
                "max_posts_per_run": 150,
            },
            "youtube": {
                "search_terms": _clean_list(draft.youtube_search_terms, MAX_YOUTUBE_TERMS, max_len=80),
                "max_videos": 10,
                "max_comments_per_video": 50,
            },
        },
        "connectors": [],
        "claim_vs_reported": metrics,
        "scoring": {
            "weights": {"market_size": 0.30, "competitor_gap": 0.25, "evidence_strength": 0.30, "effort": 0.15},
            "top_n_in_brief": 5,
        },
        "output": {"anonymize_entities": False, "brief_max_words": 350, "telegram_max_chars": 4000},
        "delivery": {"channels": ["telegram", "email"]},
        "retention_days": {"posts": 180, "documents": 365},
        "limits": {"max_llm_cost_per_run_usd": 3.0, "per_host_delay_seconds": 2.0},
    }


class _CompactDumper(yaml.SafeDumper):
    """Short scalar lists and small flat mappings in flow style, like the hand-written workspaces."""


def _is_scalar(v: Any) -> bool:
    return v is None or isinstance(v, str | int | float | bool)


def _repr_list(dumper: yaml.SafeDumper, data: list[Any]) -> yaml.Node:
    flow = all(_is_scalar(v) for v in data) and len(repr(data)) <= 100
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=flow)


def _repr_dict(dumper: yaml.SafeDumper, data: dict[str, Any]) -> yaml.Node:
    flow = bool(data) and all(_is_scalar(v) for v in data.values()) and len(repr(data)) <= 110
    return dumper.represent_mapping("tag:yaml.org,2002:map", data.items(), flow_style=flow)


_CompactDumper.add_representer(list, _repr_list)
_CompactDumper.add_representer(dict, _repr_dict)


def _comment_block(text: str, width: int = 96) -> list[str]:
    flat = " ".join(str(text).replace("\r", " ").split())
    return [f"#   {line}" for line in textwrap.wrap(flat, width=width)] or ["#"]


def render_yaml(
    config: dict[str, Any], *, description: str, checks: list[SourceCheck], drafted_by: str, when: datetime
) -> str:
    counts = {"ok": 0, "dropped": 0, "unverified": 0}
    for c in checks:
        counts[c.status] += 1
    verified = (
        f"# Sources verified {when:%Y-%m-%d}: {counts['ok']} kept, {counts['dropped']} dropped, "
        f"{counts['unverified']} unverified."
        if checks
        else "# Sources were not verified; only URL-free sources (news search phrases) were added."
    )
    header = [
        f"# FieldNote workspace: {' '.join(str(config['display_name']).split())}",
        f"# Created {when:%Y-%m-%d} (UTC) with the workspace builder (draft: {drafted_by}) from:",
        *_comment_block(description),
        verified,
        "# Review and edit freely; `fieldnote doctor -w <workspace>` re-checks every URL.",
        "# news.search_country: 'worldwide' disables the country filter; null derives it from region.",
        "",
    ]
    body = yaml.dump(
        config, Dumper=_CompactDumper, sort_keys=False, allow_unicode=True, width=110, default_flow_style=False
    )
    footer: list[str] = []
    dropped = [c for c in checks if c.status == "dropped"]
    if dropped:
        footer = ["", "# Dropped during verification:"]
        for c in dropped:
            footer.extend(_comment_block(f"{c.kind} {c.value}: {c.detail}"))
    return "\n".join(header) + body + ("\n".join(footer) + "\n" if footer else "")


def split_csv(value: Any) -> list[str]:
    """'a, b,c' or ['a', 'b'] -> ['a', 'b', 'c'] (used for editable alias/keyword cells)."""
    if value is None or (isinstance(value, float) and value != value):  # None or NaN (empty table cell)
        return []
    items = [str(v) for v in value] if isinstance(value, list | tuple) else str(value).split(",")
    return [" ".join(i.split()) for i in items if i.strip()]


@dataclass
class ReviewEdits:
    """What the review screen lets people change before a draft is saved."""

    display_name: str
    workspace: str
    perspective: str
    focal_company: str
    # (name, aliases, index into the draft's competitors or None for a new row)
    entities: list[tuple[str, list[str], int | None]]
    aspects: list[tuple[str, list[str]]]
    search_queries: list[str]
    feeds: list[str]
    pages: list[str]
    subreddits: list[str]
    reddit_terms: list[str]
    youtube_terms: list[str]


def apply_edits(config: dict[str, Any], edits: ReviewEdits) -> dict[str, Any]:
    """Return a copy of ``config`` with review-screen edits applied (validation happens in :func:`finalize`)."""
    import copy

    out = copy.deepcopy(config)
    out["display_name"] = " ".join(edits.display_name.split())[:80]
    out["workspace"] = edits.workspace.strip()
    out["perspective"] = " ".join(edits.perspective.split())[:200] or "market-level observer"
    out["focal_company"] = " ".join(edits.focal_company.split())[:80] or None
    original = config["competitors"]
    keep_pages = set(edits.pages)
    competitors = []
    for name, aliases, idx in edits.entities:
        name = " ".join(name.split())[:60]
        if len(name) < 2:
            continue
        pages = original[idx]["pages"] if idx is not None and 0 <= idx < len(original) else []
        clean_aliases = [a[:60] for a in dict.fromkeys(aliases) if len(a) >= 2 and a.lower() != name.lower()]
        competitors.append(
            {
                "name": name,
                "aliases": clean_aliases[:MAX_ALIASES],
                "pages": [p for p in pages if p["url"] in keep_pages],
            }
        )
    out["competitors"] = competitors
    aspects: list[str] = []
    aspect_keywords: dict[str, list[str]] = {}
    for name, kws in edits.aspects:
        key = _snake(name)
        if key and key != "other" and key not in aspects:
            aspects.append(key)
            clean = _clean_list([k.lower() for k in kws], 12, max_len=40)
            if clean:
                aspect_keywords[key] = clean
    out["aspects"] = aspects
    out["aspect_keywords"] = aspect_keywords
    news = out["sources"]["news"]
    news["search_queries"] = _clean_list([q.replace('"', " ") for q in edits.search_queries], MAX_QUERIES, 80, 3)
    allowed_feeds = set(config["sources"]["news"]["rss_feeds"])
    news["rss_feeds"] = [f for f in edits.feeds if f in allowed_feeds]
    allowed_subs = set(config["sources"]["reddit"]["subreddits"])
    out["sources"]["reddit"]["subreddits"] = [s for s in edits.subreddits if s in allowed_subs]
    out["sources"]["reddit"]["search_terms"] = _clean_list(edits.reddit_terms, MAX_REDDIT_TERMS)
    out["sources"]["youtube"]["search_terms"] = _clean_list(edits.youtube_terms, MAX_YOUTUBE_TERMS, max_len=80)
    return out


def finalize(
    config: dict[str, Any], *, description: str, checks: list[SourceCheck], drafted_by: str, when: datetime
) -> tuple[WorkspaceConfig, str]:
    """Validate a config dict and render it; also confirms the YAML round-trips to the same config."""
    try:
        cfg = parse_workspace(config, "<workspace builder>")
    except ConfigError as exc:
        raise BuilderError(str(exc)) from exc
    text = render_yaml(config, description=description, checks=checks, drafted_by=drafted_by, when=when)
    reparsed = parse_workspace(yaml.safe_load(text), "<workspace builder yaml>")
    if reparsed.model_dump() != cfg.model_dump():  # pragma: no cover - defensive
        raise BuilderError("internal error: rendered YAML does not round-trip")
    return cfg, text


# ---- orchestration ----------------------------------------------------------------------------------


def build_workspace(
    req: BuildRequest,
    *,
    llm: LLMClient,
    settings: Settings,
    taken: set[str],
    verify: bool = True,
    http_factory: Callable[[], PoliteHttpClient] | None = None,
    progress: Progress | None = None,
) -> BuildResult:
    """Draft, verify and assemble a workspace. Nothing is saved; see :func:`fieldnote.workspaces.save`."""
    say = progress or (lambda _m: None)
    req.validate()
    say("Drafting the workspace (entities, aspects, search phrases, candidate sources)...")
    draft, drafted_by, warnings = draft_workspace(req, llm)
    region = (req.region or draft.region or "Global").strip() or "Global"
    prof = region_profile(region)
    country = prof[0] if prof else None
    languages = [req.language] if req.language else draft.language_hints
    language = next((GDELT_LANGUAGES[h.lower()] for h in languages if h and h.lower() in GDELT_LANGUAGES), None)
    cands = collect_candidates(draft, urls_in(req.description))
    queries = [q for q in draft.news_queries if gdelt_term(q)][:MAX_QUERIES]
    if verify:
        checks, search_country = verify_candidates(
            cands,
            settings=settings,
            news_queries=queries,
            country=country,
            language=language,
            http_factory=http_factory,
            progress=say,
        )
    else:
        checks, search_country = [], country
        if cands.feeds or cands.sites or cands.pages:
            warnings.append("Verification was skipped, so no URLs were added. Run `fieldnote doctor` after adding any.")
        cands.subreddits = []
    say("Assembling and validating the workspace...")
    config = assemble_config(req, draft, checks, search_country=search_country, taken=taken, warnings=warnings)
    when = datetime.now(UTC).replace(tzinfo=None)
    _cfg, text = finalize(config, description=req.description, checks=checks, drafted_by=drafted_by, when=when)
    return BuildResult(
        request=req,
        draft=draft,
        config=config,
        checks=checks,
        warnings=warnings,
        drafted_by=drafted_by,
        yaml_text=text,
        created_at=when,
    )
