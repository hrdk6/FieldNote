"""Workspace configuration (YAML, validated by Pydantic) and environment settings.

Everything sector- or company-specific lives in workspace YAML files. This module only knows the
*shape* of a workspace, never its contents.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_]{1,62}$")
PageKind = Literal["pricing", "product", "news", "dealers"]
SCORING_CRITERIA = ("market_size", "competitor_gap", "evidence_strength", "effort")

# Region -> Google News locale defaults. Used only when a workspace does not set sources.news.locale.
_REGION_LOCALES: dict[str, dict[str, str]] = {
    "india": {"hl": "en-IN", "gl": "IN", "ceid": "IN:en"},
    "united states": {"hl": "en-US", "gl": "US", "ceid": "US:en"},
    "usa": {"hl": "en-US", "gl": "US", "ceid": "US:en"},
    "united kingdom": {"hl": "en-GB", "gl": "GB", "ceid": "GB:en"},
    "uk": {"hl": "en-GB", "gl": "GB", "ceid": "GB:en"},
    "indonesia": {"hl": "id", "gl": "ID", "ceid": "ID:id"},
    "germany": {"hl": "de", "gl": "DE", "ceid": "DE:de"},
    "global": {"hl": "en-US", "gl": "US", "ceid": "US:en"},
}


# Region -> (GDELT source-country name, default IANA timezone). Keys are lowercase; several spellings
# map to the same country. Regions not listed (e.g. "Europe", "Global") search worldwide.
REGION_PROFILES: dict[str, tuple[str, str]] = {
    "india": ("india", "Asia/Kolkata"),
    "united states": ("unitedstates", "America/New_York"),
    "united states of america": ("unitedstates", "America/New_York"),
    "usa": ("unitedstates", "America/New_York"),
    "us": ("unitedstates", "America/New_York"),
    "america": ("unitedstates", "America/New_York"),
    "united kingdom": ("unitedkingdom", "Europe/London"),
    "uk": ("unitedkingdom", "Europe/London"),
    "britain": ("unitedkingdom", "Europe/London"),
    "great britain": ("unitedkingdom", "Europe/London"),
    "england": ("unitedkingdom", "Europe/London"),
    "canada": ("canada", "America/Toronto"),
    "australia": ("australia", "Australia/Sydney"),
    "new zealand": ("newzealand", "Pacific/Auckland"),
    "ireland": ("ireland", "Europe/Dublin"),
    "germany": ("germany", "Europe/Berlin"),
    "france": ("france", "Europe/Paris"),
    "spain": ("spain", "Europe/Madrid"),
    "italy": ("italy", "Europe/Rome"),
    "netherlands": ("netherlands", "Europe/Amsterdam"),
    "sweden": ("sweden", "Europe/Stockholm"),
    "poland": ("poland", "Europe/Warsaw"),
    "japan": ("japan", "Asia/Tokyo"),
    "south korea": ("southkorea", "Asia/Seoul"),
    "korea": ("southkorea", "Asia/Seoul"),
    "china": ("china", "Asia/Shanghai"),
    "singapore": ("singapore", "Asia/Singapore"),
    "indonesia": ("indonesia", "Asia/Jakarta"),
    "malaysia": ("malaysia", "Asia/Kuala_Lumpur"),
    "thailand": ("thailand", "Asia/Bangkok"),
    "vietnam": ("vietnam", "Asia/Ho_Chi_Minh"),
    "philippines": ("philippines", "Asia/Manila"),
    "pakistan": ("pakistan", "Asia/Karachi"),
    "bangladesh": ("bangladesh", "Asia/Dhaka"),
    "sri lanka": ("srilanka", "Asia/Colombo"),
    "nepal": ("nepal", "Asia/Kathmandu"),
    "united arab emirates": ("unitedarabemirates", "Asia/Dubai"),
    "uae": ("unitedarabemirates", "Asia/Dubai"),
    "saudi arabia": ("saudiarabia", "Asia/Riyadh"),
    "israel": ("israel", "Asia/Jerusalem"),
    "turkey": ("turkey", "Europe/Istanbul"),
    "egypt": ("egypt", "Africa/Cairo"),
    "nigeria": ("nigeria", "Africa/Lagos"),
    "kenya": ("kenya", "Africa/Nairobi"),
    "south africa": ("southafrica", "Africa/Johannesburg"),
    "brazil": ("brazil", "America/Sao_Paulo"),
    "mexico": ("mexico", "America/Mexico_City"),
    "argentina": ("argentina", "America/Argentina/Buenos_Aires"),
    "colombia": ("colombia", "America/Bogota"),
    "chile": ("chile", "America/Santiago"),
}

# ISO 639-1 language hint -> GDELT source-language name.
GDELT_LANGUAGES: dict[str, str] = {
    "en": "english",
    "hi": "hindi",
    "bn": "bengali",
    "ta": "tamil",
    "te": "telugu",
    "mr": "marathi",
    "ur": "urdu",
    "id": "indonesian",
    "ms": "malay",
    "de": "german",
    "fr": "french",
    "es": "spanish",
    "pt": "portuguese",
    "it": "italian",
    "nl": "dutch",
    "pl": "polish",
    "sv": "swedish",
    "tr": "turkish",
    "ar": "arabic",
    "ja": "japanese",
    "ko": "korean",
    "zh": "chinese",
    "th": "thai",
    "vi": "vietnamese",
}


def region_profile(region: str) -> tuple[str, str] | None:
    return REGION_PROFILES.get(" ".join(region.lower().replace(".", "").split()))


class ConfigError(Exception):
    """Raised when a workspace file or environment setting is invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_http_url(value: str) -> str:
    value = value.strip()
    if not re.match(r"^https?://[^\s/$.?#][^\s]*$", value):
        raise ValueError(f"'{value}' is not a valid http(s) URL")
    return value


class PageConfig(_Strict):
    url: str
    kind: PageKind = "product"
    render: bool = False
    selector: str | None = None

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return _check_http_url(v)


class CompetitorConfig(_Strict):
    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    pages: list[PageConfig] = Field(default_factory=list)

    def all_names(self) -> list[str]:
        names = [self.name, *self.aliases]
        seen: set[str] = set()
        out: list[str] = []
        for n in names:
            key = n.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(n.strip())
        return out


class NewsLocale(_Strict):
    hl: str
    gl: str
    ceid: str


class NewsSources(_Strict):
    google_news_queries: list[str] = Field(default_factory=list)
    # Topic searches through the GDELT DOC 2.0 API (open, no key). Short headline-style phrases or names.
    search_queries: list[str] = Field(default_factory=list)
    # GDELT source-country name without spaces (e.g. "india", "unitedstates"); "worldwide" = no filter;
    # None = derived from the workspace region.
    search_country: str | None = None
    # GDELT source language name (e.g. "english", "hindi"); None = any language.
    search_language: str | None = None
    rss_feeds: list[str] = Field(default_factory=list)
    # Keep only feed items that mention an entity, alias, keyword or search phrase. Useful for broad
    # publications (general tech or business feeds); topic-specific feeds can leave it off.
    filter_feeds_by_relevance: bool = False
    locale: NewsLocale | None = None
    lookback_days: int = Field(7, ge=1, le=60)
    max_items_per_feed: int = Field(25, ge=1, le=200)
    fetch_full_text: bool = True

    @field_validator("rss_feeds")
    @classmethod
    def _feeds_are_urls(cls, v: list[str]) -> list[str]:
        return [_check_http_url(u) for u in v]

    @field_validator("search_queries")
    @classmethod
    def _queries(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for q in v:
            q = " ".join(q.replace('"', " ").split())
            if not 3 <= len(q) <= 80:
                raise ValueError(f"search query {q!r} must be 3-80 characters")
            if q.lower() not in {x.lower() for x in out}:
                out.append(q)
        return out

    @field_validator("search_country", "search_language")
    @classmethod
    def _gdelt_name(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip().lower().replace(" ", "")
        if not re.match(r"^[a-z]{3,40}$", v):
            raise ValueError(f"'{v}' must be a GDELT name in lowercase letters, e.g. 'india' or 'english'")
        return v


class RedditSources(_Strict):
    subreddits: list[str] = Field(default_factory=list)
    search_terms: list[str] = Field(default_factory=list)
    max_posts_per_run: int = Field(200, ge=1, le=1000)

    @field_validator("subreddits")
    @classmethod
    def _clean_subs(cls, v: list[str]) -> list[str]:
        out = []
        for s in v:
            s = s.strip().removeprefix("r/").removeprefix("/r/")
            if not re.match(r"^[A-Za-z0-9_]{2,40}$", s):
                raise ValueError(f"'{s}' is not a valid subreddit name")
            out.append(s)
        return out


class YoutubeSources(_Strict):
    search_terms: list[str] = Field(default_factory=list)
    max_videos: int = Field(15, ge=0, le=50)
    max_comments_per_video: int = Field(60, ge=0, le=100)
    quota_units_per_run: int = Field(1500, ge=0, le=10000)


class SourcesConfig(_Strict):
    news: NewsSources = Field(default_factory=NewsSources)
    reddit: RedditSources = Field(default_factory=RedditSources)
    youtube: YoutubeSources = Field(default_factory=YoutubeSources)


class ConnectorConfig(BaseModel):
    """A pluggable metrics connector. Extra keys are passed through to the connector class."""

    model_config = ConfigDict(extra="allow")

    type: str
    name: str | None = None
    path: str | None = None
    url: str | None = None
    mapping: dict[str, str]
    unit: str = ""
    source_url: str = ""
    records_path: str | None = None

    @model_validator(mode="after")
    def _check_mapping(self) -> ConnectorConfig:
        missing = {"entity", "period", "value"} - set(self.mapping)
        if missing:
            raise ValueError(f"connector mapping is missing required keys: {sorted(missing)}")
        return self

    @property
    def display_name(self) -> str:
        return self.name or f"{self.type}:{self.path or self.url or 'unnamed'}"


class ClaimVsReportedConfig(_Strict):
    metric: str
    unit: str
    claimed_extract: Literal["regex", "llm"] = "regex"
    reported_extract: Literal["llm", "regex"] = "llm"
    min_n: int = Field(10, ge=1)
    unit_aliases: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    plausible_range: tuple[float, float] | None = None

    def all_units(self) -> list[str]:
        units = [self.unit, *self.unit_aliases]
        return sorted({u.strip() for u in units if u.strip()}, key=len, reverse=True)

    def all_keywords(self) -> list[str]:
        return sorted({k.lower() for k in [self.metric.replace("_", " "), *self.keywords]}, key=len, reverse=True)


class ScoringConfig(_Strict):
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "market_size": 0.30,
            "competitor_gap": 0.25,
            "evidence_strength": 0.30,
            "effort": 0.15,
        }
    )
    top_n_in_brief: int = Field(5, ge=1, le=20)
    evidence_half_life_days: float = Field(14.0, gt=0)
    min_evidence_per_finding: int = Field(2, ge=1)
    stale_after_days: int = Field(21, ge=1)

    @field_validator("weights")
    @classmethod
    def _check_weights(cls, v: dict[str, float]) -> dict[str, float]:
        unknown = set(v) - set(SCORING_CRITERIA)
        if unknown:
            raise ValueError(f"unknown scoring criteria {sorted(unknown)}; allowed: {list(SCORING_CRITERIA)}")
        missing = set(SCORING_CRITERIA) - set(v)
        if missing:
            raise ValueError(f"scoring weights missing {sorted(missing)}")
        if any(w < 0 for w in v.values()):
            raise ValueError("scoring weights must be non-negative")
        total = sum(v.values())
        if total <= 0:
            raise ValueError("scoring weights must not all be zero")
        if abs(total - 1.0) > 0.01:
            raise ValueError(f"scoring weights must sum to 1.0 (got {total:.3f})")
        return {k: float(v[k]) for k in SCORING_CRITERIA}


class OutputConfig(_Strict):
    anonymize_entities: bool = False
    brief_max_words: int = Field(350, ge=80, le=2000)
    telegram_max_chars: int = Field(4000, ge=500, le=4096)


class RetentionConfig(_Strict):
    posts: int = Field(180, ge=1)
    documents: int = Field(365, ge=1)


class LimitsConfig(_Strict):
    max_llm_cost_per_run_usd: float = Field(3.0, ge=0)
    per_host_delay_seconds: float = Field(2.0, ge=0)
    researcher_max_steps: int = Field(6, ge=2, le=20)
    max_claims_per_run: int = Field(60, ge=1, le=300)
    request_timeout_seconds: float = Field(20.0, gt=0)
    max_documents_per_source: int = Field(60, ge=1, le=1000)


class ChangeDetectionConfig(_Strict):
    min_changed_chars: int = Field(20, ge=0)
    min_change_ratio: float = Field(0.01, ge=0, le=1)
    ignore_patterns: list[str] = Field(default_factory=list)

    @field_validator("ignore_patterns")
    @classmethod
    def _compile(cls, v: list[str]) -> list[str]:
        for p in v:
            try:
                re.compile(p)
            except re.error as exc:
                raise ValueError(f"invalid regex in ignore_patterns: {p!r}: {exc}") from exc
        return v


class RemindersConfig(_Strict):
    due_within_days: int = Field(2, ge=0, le=60)


class DeliveryConfig(_Strict):
    channels: list[Literal["telegram", "email"]] = Field(default_factory=list)


class WorkspaceConfig(_Strict):
    workspace: str
    display_name: str
    sector: str
    region: str
    timezone: str = "UTC"
    language_hints: list[str] = Field(default_factory=lambda: ["en"])
    focal_company: str | None = None
    perspective: str
    competitors: list[CompetitorConfig] = Field(min_length=1)
    keywords: list[str] = Field(default_factory=list)
    aspects: list[str] = Field(min_length=1)
    aspect_keywords: dict[str, list[str]] = Field(default_factory=dict)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    connectors: list[ConnectorConfig] = Field(default_factory=list)
    claim_vs_reported: list[ClaimVsReportedConfig] = Field(default_factory=list)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    retention_days: RetentionConfig = Field(default_factory=RetentionConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    change_detection: ChangeDetectionConfig = Field(default_factory=ChangeDetectionConfig)
    reminders: RemindersConfig = Field(default_factory=RemindersConfig)
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)

    # Set at load time; not part of the YAML schema.
    source_path: str | None = Field(default=None, exclude=True)
    fixture_mode: bool = Field(default=False, exclude=True)

    @field_validator("workspace")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not SLUG_RE.match(v):
            raise ValueError("workspace must be a lowercase slug: letters, digits and underscores (2-63 chars)")
        return v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone '{v}' (use an IANA name such as 'Asia/Kolkata')") from exc
        return v

    @field_validator("aspects")
    @classmethod
    def _aspects(cls, v: list[str]) -> list[str]:
        out = []
        for a in v:
            a = a.strip().lower().replace(" ", "_")
            if not re.match(r"^[a-z][a-z0-9_]{0,40}$", a):
                raise ValueError(f"aspect '{a}' must be a short snake_case label")
            if a == "other":
                continue
            out.append(a)
        if len(set(out)) != len(out):
            raise ValueError("aspects must be unique")
        return out

    @model_validator(mode="after")
    def _cross_checks(self) -> WorkspaceConfig:
        names: dict[str, str] = {}
        for comp in self.competitors:
            for n in comp.all_names():
                key = n.lower()
                if key in names and names[key] != comp.name:
                    raise ValueError(f"name/alias '{n}' is used by both '{names[key]}' and '{comp.name}'")
                names[key] = comp.name
        unknown = set(self.aspect_keywords) - set(self.aspects)
        if unknown:
            raise ValueError(f"aspect_keywords references unknown aspects: {sorted(unknown)}")
        metrics = [c.metric for c in self.claim_vs_reported]
        if len(set(metrics)) != len(metrics):
            raise ValueError("claim_vs_reported metrics must be unique")
        return self

    # ---- convenience -------------------------------------------------------------------------
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def local_now(self, utc_naive: datetime) -> datetime:
        """Convert a naive-UTC timestamp to the workspace timezone."""
        return utc_naive.replace(tzinfo=UTC).astimezone(self.tz)

    def local_date(self, utc_naive: datetime) -> date:
        return self.local_now(utc_naive).date()

    def news_locale(self) -> NewsLocale:
        if self.sources.news.locale:
            return self.sources.news.locale
        return NewsLocale(**_REGION_LOCALES.get(self.region.strip().lower(), _REGION_LOCALES["global"]))

    def gdelt_country(self) -> str | None:
        """GDELT source-country filter: explicit setting, else derived from the region (None = worldwide)."""
        country = self.sources.news.search_country
        if country == "worldwide":
            return None
        if country:
            return country
        prof = region_profile(self.region)
        return prof[0] if prof else None

    def gdelt_language(self) -> str | None:
        """GDELT source-language filter: explicit setting, else the first plain language hint."""
        if self.sources.news.search_language:
            return self.sources.news.search_language
        for hint in self.language_hints:
            name = GDELT_LANGUAGES.get(hint.strip().lower())
            if name:
                return name
        return None

    def competitor_names(self) -> list[str]:
        return [c.name for c in self.competitors]

    def alias_map(self) -> dict[str, str]:
        """lowercased alias -> canonical competitor name."""
        out: dict[str, str] = {}
        for comp in self.competitors:
            for n in comp.all_names():
                out[n.lower()] = comp.name
        return out

    def entity_aliases(self) -> dict[str, str]:
        """alias (original casing) -> canonical competitor name, for entity matching."""
        out: dict[str, str] = {}
        for comp in self.competitors:
            for n in comp.all_names():
                out[n] = comp.name
        return out

    def aspect_term_map(self) -> dict[str, list[str]]:
        return {a: self.aspect_terms(a) for a in self.aspects}

    def aspect_terms(self, aspect: str) -> list[str]:
        base = [aspect.replace("_", " ")]
        return sorted({t.lower() for t in base + self.aspect_keywords.get(aspect, [])}, key=len, reverse=True)


# ---------------------------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------------------------

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parents[1]


def _resolve_dir(env_name: str, default_name: str) -> Path:
    env = os.environ.get(env_name)
    if env:
        return Path(env).expanduser().resolve()
    cwd_candidate = Path.cwd() / default_name
    if cwd_candidate.exists():
        return cwd_candidate.resolve()
    return (REPO_ROOT / default_name).resolve()


def workspaces_dir() -> Path:
    return _resolve_dir("FIELDNOTE_WORKSPACES_DIR", "workspaces")


def fixtures_dir() -> Path:
    return _resolve_dir("FIELDNOTE_FIXTURES_DIR", "fixtures")


def out_dir() -> Path:
    env = os.environ.get("FIELDNOTE_OUT_DIR")
    path = Path(env).expanduser() if env else Path.cwd() / "out"
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def data_dir() -> Path:
    env = os.environ.get("FIELDNOTE_DATA_DIR")
    path = Path(env).expanduser() if env else Path.cwd() / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def workspace_out_dir(workspace: str) -> Path:
    if not SLUG_RE.match(workspace):
        raise ConfigError(f"invalid workspace slug: {workspace!r}")
    path = out_dir() / workspace
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_join(base: Path, *parts: str) -> Path:
    """Join path parts under ``base`` and refuse anything that escapes it (path traversal)."""
    base = base.resolve()
    candidate = base.joinpath(*parts).resolve()
    if candidate != base and base not in candidate.parents:
        raise ConfigError(f"path {candidate} escapes allowed directory {base}")
    return candidate


# ---------------------------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------------------------


def _format_validation_error(path: Path | str, exc: ValidationError) -> str:
    lines = [f"Invalid workspace config {path}:"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
        msg = err.get("msg", "invalid value")
        lines.append(f"  - {loc}: {msg}")
    return "\n".join(lines)


def parse_workspace(data: dict[str, Any], source: str = "<dict>") -> WorkspaceConfig:
    try:
        cfg = WorkspaceConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(source, exc)) from exc
    cfg.source_path = source
    return cfg


def load_workspace_file(path: Path) -> WorkspaceConfig:
    if not path.exists():
        raise ConfigError(f"workspace file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: YAML syntax error: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    cfg = parse_workspace(data, str(path))
    if path.stem != cfg.workspace and not path.stem.startswith("_"):
        raise ConfigError(f"{path}: file name must match the 'workspace' slug ('{cfg.workspace}.yaml')")
    return cfg


def list_workspaces() -> list[str]:
    d = workspaces_dir()
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.yaml") if not p.stem.startswith("_"))


def load_workspace(name_or_path: str | None = None) -> WorkspaceConfig:
    """Load a workspace by slug (from the workspaces directory) or by explicit YAML path."""
    name = name_or_path or get_settings().default_workspace
    if name.endswith((".yaml", ".yml")) or os.sep in name or "/" in name:
        return load_workspace_file(Path(name).expanduser().resolve())
    if not SLUG_RE.match(name):
        raise ConfigError(f"invalid workspace name {name!r}")
    path = workspaces_dir() / f"{name}.yaml"
    if not path.exists():
        available = ", ".join(list_workspaces()) or "(none)"
        raise ConfigError(f"workspace '{name}' not found in {workspaces_dir()}. Available: {available}")
    return load_workspace_file(path)


def apply_fixture_overlay(cfg: WorkspaceConfig) -> WorkspaceConfig:
    """Return a copy of ``cfg`` adjusted for offline fixture mode.

    Fixture datasets use fictional brand names so demo output can never be mistaken for real market
    claims. The overlay (``fixtures/<workspace>/manifest.json``) swaps in those competitors and points
    connectors at bundled CSV files. Everything else (aspects, scoring, output settings) is kept.
    """
    manifest_path = fixtures_dir() / cfg.workspace / "manifest.json"
    if not manifest_path.exists():
        raise ConfigError(f"no fixtures for workspace '{cfg.workspace}' (expected {manifest_path})")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    data = cfg.model_dump(mode="json")
    overlay = manifest.get("overlay", {})
    for key in ("competitors", "connectors", "aspect_keywords", "claim_vs_reported"):
        if key in overlay:
            data[key] = overlay[key]
    # Connector paths in fixtures are relative to the fixtures workspace directory.
    for conn in data.get("connectors", []):
        if conn.get("path") and not Path(conn["path"]).is_absolute():
            conn["path"] = str(safe_join(fixtures_dir() / cfg.workspace, conn["path"]))
    new_cfg = parse_workspace(data, f"{cfg.source_path} (+fixture overlay)")
    new_cfg.fixture_mode = True
    return new_cfg


# ---------------------------------------------------------------------------------------------
# Environment settings
# ---------------------------------------------------------------------------------------------

SECRET_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "NVIDIA_API_KEY",
    "GROQ_API_KEY",
    "REDDIT_CLIENT_SECRET",
    "YOUTUBE_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "SMTP_PASSWORD",
    "FIELDNOTE_DASHBOARD_PASSWORD",
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    "DATABASE_URL",
)


@lru_cache(maxsize=1)
def _load_dotenv_once() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dependency is declared
        return
    env_path = Path.cwd() / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _bool_env(name: str) -> bool:
    return (_env(name, "") or "").lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return max(1, int(raw))
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


# Free-tier LLM providers (OpenAI-compatible endpoints), in the order FIELDNOTE_LLM=auto chains them.
# Model lists are best first; the next model (then the next provider) takes over when one is out of
# quota, overloaded or unavailable. Chosen from live structured-output tests (see docs/assumptions.md).
FREE_PROVIDERS: tuple[str, ...] = ("gemini", "nvidia", "groq")
DEFAULT_PROVIDER_MODELS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "gemini": (
        ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash", "gemini-2.5-flash"),
        ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite"),
    ),
    "nvidia": (
        ("nvidia/nemotron-3-ultra-550b-a55b", "nvidia/nemotron-3-super-120b-a12b"),
        ("nvidia/nemotron-3-super-120b-a12b",),
    ),
    "groq": (
        ("openai/gpt-oss-120b", "openai/gpt-oss-20b"),
        ("openai/gpt-oss-20b",),
    ),
}
DEFAULT_PROVIDER_RPM: dict[str, int] = {"gemini": 8, "nvidia": 30, "groq": 25}


@dataclass(frozen=True)
class ProviderSettings:
    """One OpenAI-compatible provider: key, main-tier and fast-tier models (best first), request pacing."""

    name: str
    api_key: str | None
    models: tuple[str, ...]
    fast_models: tuple[str, ...]
    rpm: int

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def model_pairs(self) -> list[tuple[str, str]]:
        """(main, fast) pairs in fallback order; the shorter list repeats its last model."""
        n = max(len(self.models), len(self.fast_models))
        return [
            (self.models[min(i, len(self.models) - 1)], self.fast_models[min(i, len(self.fast_models) - 1)])
            for i in range(n)
        ]


def _model_list(prefix: str, fast: bool, default: tuple[str, ...]) -> tuple[str, ...]:
    """``<P>_MODELS`` / ``<P>_FAST_MODELS`` (comma-separated, best first); a single ``<P>_MODEL`` still works."""
    suffix = "FAST_MODEL" if fast else "MODEL"
    plural = _env(f"{prefix}_{suffix}S")
    if plural:
        items = tuple(dict.fromkeys(m.strip() for m in plural.split(",") if m.strip()))
        if items:
            return items
    single = _env(f"{prefix}_{suffix}")
    return (single,) if single else default


def _provider_settings(name: str) -> ProviderSettings:
    prefix = name.upper()
    main, fast = DEFAULT_PROVIDER_MODELS[name]
    return ProviderSettings(
        name=name,
        api_key=_env(f"{prefix}_API_KEY"),
        models=_model_list(prefix, False, main),
        fast_models=_model_list(prefix, True, fast),
        rpm=_int_env(f"{prefix}_RPM", DEFAULT_PROVIDER_RPM[name]),
    )


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str | None
    model: str
    fast_model: str
    llm_provider: str
    database_url: str | None
    reddit_client_id: str | None
    reddit_client_secret: str | None
    reddit_user_agent: str | None
    youtube_api_key: str | None
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    smtp_host: str | None
    smtp_port: int
    smtp_user: str | None
    smtp_password: str | None
    smtp_from: str | None
    smtp_to: tuple[str, ...]
    smtp_security: str
    contact_email: str | None
    google_service_account_json: str | None
    google_sheets_spreadsheet_id: str | None
    default_workspace: str
    log_level: str
    log_format: str
    # Free-tier alternatives to Anthropic (OpenAI-compatible endpoints), in FREE_PROVIDERS order.
    free_providers: tuple[ProviderSettings, ...] = ()
    # Fetching private/loopback/link-local addresses is refused unless explicitly allowed (SSRF guard).
    allow_private_urls: bool = False
    # Optional dashboard access control for hosted deployments.
    dashboard_password: str | None = None
    dashboard_readonly: bool = False

    @property
    def user_agent(self) -> str:
        contact = self.contact_email or "contact-not-configured"
        return f"FieldNoteBot/1.0 (+{contact})"

    @property
    def has_anthropic(self) -> bool:
        return bool(self.anthropic_api_key)

    def provider(self, name: str) -> ProviderSettings:
        for p in self.free_providers:
            if p.name == name:
                return p
        main, fast = DEFAULT_PROVIDER_MODELS[name]
        return ProviderSettings(name, None, main, fast, DEFAULT_PROVIDER_RPM[name])

    @property
    def has_gemini(self) -> bool:
        return self.provider("gemini").configured

    @property
    def has_nvidia(self) -> bool:
        return self.provider("nvidia").configured

    @property
    def has_groq(self) -> bool:
        return self.provider("groq").configured

    @property
    def has_live_llm(self) -> bool:
        return self.has_anthropic or any(p.configured for p in self.free_providers)

    @property
    def has_reddit(self) -> bool:
        return bool(self.reddit_client_id and self.reddit_client_secret)

    @property
    def has_youtube(self) -> bool:
        return bool(self.youtube_api_key)

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def has_email(self) -> bool:
        return bool(self.smtp_host and self.smtp_from and self.smtp_to)

    @property
    def has_sheets(self) -> bool:
        return bool(self.google_service_account_json and self.google_sheets_spreadsheet_id)


def get_settings() -> Settings:
    _load_dotenv_once()
    smtp_to = tuple(a.strip() for a in (_env("SMTP_TO", "") or "").split(",") if a.strip())
    port_raw = _env("SMTP_PORT", "587") or "587"
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise ConfigError(f"SMTP_PORT must be an integer, got {port_raw!r}") from exc
    return Settings(
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        model=_env("FIELDNOTE_MODEL", "claude-sonnet-5") or "claude-sonnet-5",
        fast_model=_env("FIELDNOTE_FAST_MODEL", "claude-haiku-4-5-20251001") or "claude-haiku-4-5-20251001",
        llm_provider=(_env("FIELDNOTE_LLM", "auto") or "auto").lower(),
        database_url=_env("DATABASE_URL"),
        reddit_client_id=_env("REDDIT_CLIENT_ID"),
        reddit_client_secret=_env("REDDIT_CLIENT_SECRET"),
        reddit_user_agent=_env("REDDIT_USER_AGENT"),
        youtube_api_key=_env("YOUTUBE_API_KEY"),
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
        smtp_host=_env("SMTP_HOST"),
        smtp_port=port,
        smtp_user=_env("SMTP_USER"),
        smtp_password=_env("SMTP_PASSWORD"),
        smtp_from=_env("SMTP_FROM"),
        smtp_to=smtp_to,
        smtp_security=(_env("SMTP_SECURITY", "starttls") or "starttls").lower(),
        contact_email=_env("FIELDNOTE_CONTACT_EMAIL"),
        google_service_account_json=_env("GOOGLE_SERVICE_ACCOUNT_JSON"),
        google_sheets_spreadsheet_id=_env("GOOGLE_SHEETS_SPREADSHEET_ID"),
        default_workspace=_env("FIELDNOTE_WORKSPACE", "ev_two_wheelers_india") or "ev_two_wheelers_india",
        log_level=(_env("FIELDNOTE_LOG_LEVEL", "INFO") or "INFO").upper(),
        log_format=(_env("FIELDNOTE_LOG_FORMAT", "text") or "text").lower(),
        free_providers=tuple(_provider_settings(name) for name in FREE_PROVIDERS),
        allow_private_urls=_bool_env("FIELDNOTE_ALLOW_PRIVATE_URLS"),
        dashboard_password=_env("FIELDNOTE_DASHBOARD_PASSWORD"),
        dashboard_readonly=_bool_env("FIELDNOTE_DASHBOARD_READONLY"),
    )


def resolve_database_url(offline: bool = False) -> str:
    """DATABASE_URL wins; otherwise live runs use data/fieldnote.db and offline/demo runs data/demo.db."""
    env = get_settings().database_url
    if env:
        return env
    name = "demo.db" if offline else "fieldnote.db"
    return f"sqlite:///{(data_dir() / name).as_posix()}"
