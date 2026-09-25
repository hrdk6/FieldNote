"""`fieldnote doctor`: validate configs, environment, credentials, source reachability and robots status."""

from __future__ import annotations

import concurrent.futures as cf
import importlib.util
from dataclasses import dataclass

import feedparser
import requests

from fieldnote.collectors.base import PoliteHttpClient, RobotsDisallowedError
from fieldnote.collectors.rss_news import google_news_url
from fieldnote.config import (
    ConfigError,
    Settings,
    WorkspaceConfig,
    fixtures_dir,
    get_settings,
    resolve_database_url,
)
from fieldnote.db.engine import get_database


@dataclass
class Check:
    check: str
    status: str  # pass | warn | fail
    detail: str


def _llm_detail(s: Settings) -> str:
    parts = []
    if s.has_anthropic:
        parts.append(f"anthropic ({s.model} / {s.fast_model})")
    for p in s.free_providers:
        if p.configured:
            parts.append(f"{p.name} ({' > '.join(p.models)}; fast: {' > '.join(p.fast_models)}; {p.rpm} rpm)")
    missing = [p.name for p in s.free_providers if not p.configured]
    if not parts:
        return (
            "no ANTHROPIC_API_KEY / GEMINI_API_KEY / NVIDIA_API_KEY / GROQ_API_KEY: "
            "MockClient will be used (offline-quality output)"
        )
    note = f"; not configured: {', '.join(missing)}" if missing else ""
    return f"FIELDNOTE_LLM={s.llm_provider}; fallback order: " + " -> ".join(parts) + note


def _env_checks() -> list[Check]:
    s = get_settings()
    out = [
        Check(
            "LLM provider",
            "pass" if s.has_live_llm else "warn",
            _llm_detail(s),
        ),
        Check(
            "Reddit API",
            "pass" if s.has_reddit else "warn",
            "credentials present"
            if s.has_reddit
            else "REDDIT_CLIENT_ID/SECRET not set: Reddit collector will be skipped",
        ),
        Check(
            "YouTube API",
            "pass" if s.has_youtube else "warn",
            "key present" if s.has_youtube else "YOUTUBE_API_KEY not set: YouTube collector will be skipped",
        ),
        Check(
            "Telegram delivery",
            "pass" if s.has_telegram else "warn",
            "configured" if s.has_telegram else "not configured: deliveries go to the dry-run outbox",
        ),
        Check(
            "Email delivery",
            "pass" if s.has_email else "warn",
            f"{s.smtp_host}:{s.smtp_port} ({s.smtp_security})"
            if s.has_email
            else "SMTP_* not configured: dry-run outbox",
        ),
        Check(
            "Contact for User-Agent",
            "pass" if s.contact_email else "warn",
            s.user_agent if s.contact_email else "FIELDNOTE_CONTACT_EMAIL not set; crawlers should identify a contact",
        ),
        Check(
            "Google Sheets sync", "pass" if s.has_sheets else "warn", "configured" if s.has_sheets else "off (optional)"
        ),
        Check(
            "Playwright (render: true pages)",
            "pass" if importlib.util.find_spec("playwright") else "warn",
            "installed"
            if importlib.util.find_spec("playwright")
            else "not installed; pages with render: true use plain fetch",
        ),
    ]
    try:
        db = get_database(resolve_database_url())
        out.append(Check("Database", "pass", db.url.split("@")[-1]))
    except Exception as exc:  # pragma: no cover - environment specific
        out.append(Check("Database", "fail", f"{type(exc).__name__}: {exc}"))
    return out


def _probe(http: PoliteHttpClient, label: str, url: str, is_feed: bool) -> Check:
    try:
        if not http.allowed(url):
            return Check(
                label, "warn", f"robots.txt disallows FieldNoteBot ({http.robots_status(url)}); will be skipped"
            )
        res = http.get(url, conditional=False)
    except RobotsDisallowedError:
        return Check(label, "warn", "robots.txt disallows; will be skipped")
    except requests.RequestException as exc:
        return Check(label, "fail", f"unreachable: {type(exc).__name__}")
    if not res.ok:
        return Check(label, "fail", f"HTTP {res.status}")
    if is_feed:
        n = len(feedparser.parse(res.text).entries)
        if n == 0:
            return Check(label, "fail", "reachable but no feed entries (not a valid RSS/Atom feed)")
        return Check(label, "pass", f"{n} entries · robots allowed")
    return Check(label, "pass", f"HTTP {res.status} · {len(res.text):,} chars · robots allowed")


def _source_checks(cfg: WorkspaceConfig) -> list[Check]:
    s = get_settings()
    targets: list[tuple[str, str, bool]] = []
    loc = cfg.news_locale()
    if cfg.sources.news.google_news_queries:
        q = cfg.sources.news.google_news_queries[0]
        targets.append((f"[{cfg.workspace}] Google News RSS", google_news_url(q, loc.hl, loc.gl, loc.ceid, 7), True))
    for f in cfg.sources.news.rss_feeds:
        targets.append((f"[{cfg.workspace}] feed {f}", f, True))
    for c in cfg.competitors:
        for p in c.pages:
            targets.append((f"[{cfg.workspace}] page {c.name}: {p.url}", p.url, False))

    def run(t: tuple[str, str, bool]) -> Check:
        http = PoliteHttpClient(
            s,
            per_host_delay=cfg.limits.per_host_delay_seconds,
            timeout=cfg.limits.request_timeout_seconds,
            max_retries=1,
        )
        return _probe(http, *t)

    def search() -> Check:
        from fieldnote.builder.verify import probe_news_search

        http = PoliteHttpClient(s, per_host_delay=cfg.limits.per_host_delay_seconds, timeout=30.0, max_retries=1)
        probe = probe_news_search(http, cfg.sources.news.search_queries, cfg.gdelt_country(), cfg.gdelt_language(), 7)
        status = {"ok": "pass", "unverified": "warn", "dropped": "fail"}[probe.check.status]
        detail = probe.check.detail
        if cfg.gdelt_country() and probe.country is None:
            status = "warn"
            detail += " (set news.search_country: worldwide to search all sources)"
        return Check(f"[{cfg.workspace}] news search (GDELT)", status, detail)

    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        futures = [ex.submit(run, t) for t in targets]
        search_future = ex.submit(search) if cfg.sources.news.search_queries else None
        rows = [f.result() for f in futures]
        if search_future is not None:
            rows.insert(0, search_future.result())
        return rows


def run_doctor(workspace: str | None = None, *, no_network: bool = False, check_llm: bool = False) -> list[Check]:
    from fieldnote.workspaces import list_names, load

    rows: list[Check] = []
    names = [workspace] if workspace else list_names()
    configs: list[WorkspaceConfig] = []
    for name in names:
        try:
            cfg = load(name)
            configs.append(cfg)
            n_pages = sum(len(c.pages) for c in cfg.competitors)
            rows.append(
                Check(
                    f"config {cfg.workspace}",
                    "pass",
                    f"{len(cfg.competitors)} competitors, {n_pages} pages, {len(cfg.sources.news.rss_feeds)} feeds, "
                    f"{len(cfg.sources.news.search_queries)} news searches, {len(cfg.aspects)} aspects, "
                    f"{len(cfg.connectors)} connectors",
                )
            )
            fx = fixtures_dir() / cfg.workspace / "manifest.json"
            rows.append(
                Check(
                    f"fixtures {cfg.workspace}",
                    "pass" if fx.exists() else "warn",
                    "offline demo data present"
                    if fx.exists()
                    else "no fixtures: --offline unavailable (live runs only)",
                )
            )
            for conn in cfg.connectors:
                if conn.type == "csv_file" and conn.path:
                    from fieldnote.collectors.connectors.base import resolve_data_path

                    try:
                        resolve_data_path(conn.path, cfg)
                        rows.append(Check(f"connector {conn.display_name}", "pass", f"file {conn.path} found"))
                    except ConfigError:
                        rows.append(
                            Check(
                                f"connector {conn.display_name}",
                                "warn",
                                f"{conn.path} not found: metrics skipped until you add it (see docs/sources-and-compliance.md)",
                            )
                        )
        except ConfigError as exc:
            rows.append(Check(f"config {name}", "fail", str(exc).replace("\n", " ")))
    rows += _env_checks()
    if check_llm:
        s = get_settings()
        if s.has_live_llm:
            from fieldnote.llm.base import make_llm

            try:
                ok, msg = make_llm(s).healthcheck()
            except Exception as exc:  # a misconfigured provider should show up as a failed check
                ok, msg = False, str(exc)[:200]
            rows.append(Check("LLM API call", "pass" if ok else "fail", msg))
        else:
            rows.append(Check("LLM API call", "warn", "skipped: no LLM key"))
    if not no_network:
        for cfg in configs:
            rows += _source_checks(cfg)
    return rows
