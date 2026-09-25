"""Verify every source a workspace draft proposes before it is saved.

Nothing unverified reaches a workspace file: feeds must parse with entries, pages must return enough
text, publication sites are searched for their RSS/Atom feed (``<link rel="alternate">`` or ``/feed``),
the GDELT news search is probed once, and subreddits are checked through the official Reddit API when
credentials exist. robots.txt, the per-host rate limit and the private-address guard of
:class:`~fieldnote.collectors.base.PoliteHttpClient` apply to every request.
"""

from __future__ import annotations

import concurrent.futures as cf
import functools
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit

import feedparser
import requests
from bs4 import BeautifulSoup

from fieldnote.collectors.base import PoliteHttpClient, RobotsDisallowedError, UnsafeUrlError
from fieldnote.collectors.gdelt import GDELT_DOC_API, GdeltError, build_query, parse_response, search_params
from fieldnote.config import Settings
from fieldnote.logging_setup import get_logger
from fieldnote.processing.clean import extract_main_text
from fieldnote.textutil import canonical_url

log = get_logger(__name__)

CheckKind = Literal["feed", "site", "page", "news_search", "subreddit"]
CheckStatus = Literal["ok", "dropped", "unverified"]
Progress = Callable[[str], None]

MIN_PAGE_CHARS = 200
STALE_FEED_DAYS = 180
_FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json", "application/xml", "text/xml")


@dataclass
class SourceCheck:
    kind: CheckKind
    value: str
    status: CheckStatus
    detail: str
    origin: Literal["model", "user", "heuristic"] = "model"
    entity: str | None = None
    page_kind: str | None = None
    feed_url: str | None = None  # the verified feed for kind="feed" or a site whose feed was discovered

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class Candidates:
    feeds: list[tuple[str, str]] = field(default_factory=list)  # (url, origin)
    sites: list[tuple[str, str, str | None]] = field(default_factory=list)  # (url, origin, entity)
    pages: list[tuple[str, str, str, str]] = field(default_factory=list)  # (url, origin, entity, kind)
    subreddits: list[str] = field(default_factory=list)


@dataclass
class NewsProbe:
    check: SourceCheck
    country: str | None


def _looks_like_feed(text: str, content_type: str) -> bool:
    head = text.lstrip()[:400].lower()
    return (
        any(t in content_type.lower() for t in ("rss", "atom", "xml"))
        or head.startswith("<?xml")
        or "<rss" in head
        or "<feed" in head
    )


def _feed_detail(text: str, now: datetime) -> tuple[bool, str]:
    parsed = feedparser.parse(text)
    entries = parsed.entries
    if not entries:
        return False, "not an RSS/Atom feed (no entries)"
    newest: datetime | None = None
    for e in entries:
        tm = e.get("published_parsed") or e.get("updated_parsed")
        if tm:
            dt = datetime(*tm[:6])
            newest = dt if newest is None or dt > newest else newest
    if newest is not None and (now - newest).days > STALE_FEED_DAYS:
        return False, f"feed looks inactive (newest item {newest:%Y-%m-%d})"
    when = f", newest {newest:%Y-%m-%d}" if newest else ""
    return True, f"{len(entries)} items{when}"


def _fetch(http: PoliteHttpClient, url: str) -> tuple[Any | None, str]:
    """(FetchResult or None, failure reason)."""
    try:
        res = http.get(url, conditional=False)
    except RobotsDisallowedError:
        return None, "robots.txt disallows FieldNoteBot"
    except UnsafeUrlError as exc:
        return None, f"refused: {exc}"
    except requests.RequestException as exc:
        return None, f"unreachable ({type(exc).__name__})"
    if not res.ok:
        return None, f"HTTP {res.status}"
    return res, ""


def check_feed(http: PoliteHttpClient, url: str, now: datetime, origin: str = "model") -> SourceCheck:
    res, why = _fetch(http, url)
    if res is None:
        return SourceCheck("feed", url, "dropped", why, origin=origin)  # type: ignore[arg-type]
    ok, detail = _feed_detail(res.text, now)
    final = res.final_url or url
    return SourceCheck(
        "feed",
        url,
        "ok" if ok else "dropped",
        detail,
        origin=origin,  # type: ignore[arg-type]
        feed_url=final if ok and final.startswith("http") else (url if ok else None),
    )


def discover_feed_links(html: str, base_url: str) -> list[str]:
    """RSS/Atom URLs advertised by a page via ``<link rel="alternate" type="application/rss+xml">``."""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # pragma: no cover - parser availability
        soup = BeautifulSoup(html, "html.parser")
    out: list[str] = []
    for link in soup.find_all("link"):
        rel = " ".join(link.get("rel") or []).lower()
        typ = str(link.get("type") or "").lower()
        href = str(link.get("href") or "").strip()
        if "alternate" in rel and typ in _FEED_TYPES and href and "comments" not in href.lower():
            full = urljoin(base_url, href)
            if full.startswith(("http://", "https://")) and full not in out:
                out.append(full)
    return out[:3]


def check_site(http: PoliteHttpClient, url: str, now: datetime, origin: str, entity: str | None) -> SourceCheck:
    """Find and verify the RSS/Atom feed of a publication or company site."""
    res, why = _fetch(http, url)
    if res is None:
        return SourceCheck("site", url, "dropped", why, origin=origin, entity=entity)  # type: ignore[arg-type]
    if _looks_like_feed(res.text, res.headers.get("Content-Type", "")):
        ok, detail = _feed_detail(res.text, now)
        return SourceCheck(
            "site",
            url,
            "ok" if ok else "dropped",
            f"is a feed: {detail}" if ok else detail,
            origin=origin,  # type: ignore[arg-type]
            entity=entity,
            feed_url=url if ok else None,
        )
    candidates = discover_feed_links(res.text, res.final_url or url)
    parts = urlsplit(res.final_url or url)
    fallback = f"{parts.scheme}://{parts.netloc}/feed"
    if fallback not in candidates:
        candidates.append(fallback)
    last = "no RSS/Atom feed found"
    for cand in candidates:
        chk = check_feed(http, cand, now)
        if chk.status == "ok":
            return SourceCheck(
                "site",
                url,
                "ok",
                f"feed found: {chk.detail}",
                origin=origin,  # type: ignore[arg-type]
                entity=entity,
                feed_url=chk.feed_url or cand,
            )
        if cand != fallback:
            last = f"advertised feed failed: {chk.detail}"
    return SourceCheck("site", url, "dropped", last, origin=origin, entity=entity)  # type: ignore[arg-type]


def check_page(http: PoliteHttpClient, url: str, origin: str, entity: str, kind: str) -> SourceCheck:
    res, why = _fetch(http, url)
    if res is None:
        return SourceCheck("page", url, "dropped", why, origin=origin, entity=entity, page_kind=kind)  # type: ignore[arg-type]
    text = extract_main_text(res.text, url=url) if res.text else ""
    if len(text) < MIN_PAGE_CHARS:
        return SourceCheck(
            "page",
            url,
            "dropped",
            f"too little readable text ({len(text)} chars; the page may need JavaScript)",
            origin=origin,  # type: ignore[arg-type]
            entity=entity,
            page_kind=kind,
        )
    return SourceCheck("page", url, "ok", f"{len(text):,} chars of text", origin=origin, entity=entity, page_kind=kind)  # type: ignore[arg-type]


def probe_news_search(
    http: PoliteHttpClient, queries: list[str], country: str | None, language: str | None, lookback_days: int
) -> NewsProbe:
    """One GDELT request (two if the country filter returns nothing)."""
    label = ", ".join(queries) or "(none)"
    query = build_query(queries, country, language)
    if query is None:
        return NewsProbe(
            SourceCheck("news_search", label, "dropped", "no usable terms (each needs 3+ characters)"), country
        )

    def run(q: str) -> tuple[int | None, str]:
        try:
            res = http.get(GDELT_DOC_API, conditional=False, params=search_params(q, max(lookback_days, 14), 25))
        except (RobotsDisallowedError, requests.RequestException) as exc:
            return None, f"news search unreachable right now ({type(exc).__name__}); queries kept"
        if not res.ok:
            return None, f"news search returned HTTP {res.status} (GDELT may be rate limiting); queries kept"
        try:
            return len(parse_response(res.text)), ""
        except GdeltError as exc:
            return None, f"GDELT rejected the query: {exc}"

    n, err = run(query)
    if n is None:
        status = "dropped" if err.startswith("GDELT rejected") else "unverified"
        return NewsProbe(SourceCheck("news_search", label, status, err), country)  # type: ignore[arg-type]
    if n == 0 and country:
        wide = build_query(queries, None, language)
        n2, _err2 = run(wide) if wide else (None, "")
        if n2:
            return NewsProbe(
                SourceCheck(
                    "news_search",
                    label,
                    "ok",
                    f"no recent articles from {country} sources; searching worldwide ({n2} recent articles)",
                ),
                None,
            )
    if n == 0:
        return NewsProbe(
            SourceCheck("news_search", label, "ok", "valid query, but no articles in the last 14 days"), country
        )
    scope = f"{country} sources" if country else "worldwide"
    return NewsProbe(SourceCheck("news_search", label, "ok", f"{n}+ recent articles ({scope})"), country)


def check_subreddits(names: list[str], settings: Settings) -> list[SourceCheck]:
    valid = [n for n in names if re.fullmatch(r"[A-Za-z0-9_]{2,40}", n)]
    out = [SourceCheck("subreddit", n, "dropped", "not a valid subreddit name") for n in names if n not in valid]
    if not valid:
        return out
    if not settings.has_reddit:
        return out + [
            SourceCheck("subreddit", n, "unverified", "kept; add Reddit API keys to verify and collect it")
            for n in valid
        ]
    try:
        import praw

        reddit = praw.Reddit(
            client_id=settings.reddit_client_id,
            client_secret=settings.reddit_client_secret,
            user_agent=settings.reddit_user_agent or f"{settings.user_agent} (read-only research)",
            check_for_async=False,
        )
        reddit.read_only = True
    except Exception as exc:
        return out + [
            SourceCheck("subreddit", n, "unverified", f"Reddit API unavailable ({type(exc).__name__})") for n in valid
        ]
    for n in valid:
        try:
            sub = reddit.subreddit(n)
            subscribers = int(getattr(sub, "subscribers", 0) or 0)
            if getattr(sub, "over18", False):
                out.append(SourceCheck("subreddit", n, "dropped", "marked NSFW"))
            else:
                out.append(SourceCheck("subreddit", n, "ok", f"{subscribers:,} members"))
        except Exception as exc:
            out.append(SourceCheck("subreddit", n, "dropped", f"not found or private ({type(exc).__name__})"))
    return out


def verify_candidates(
    cands: Candidates,
    *,
    settings: Settings,
    news_queries: list[str],
    country: str | None,
    language: str | None,
    lookback_days: int = 7,
    http_factory: Callable[[], PoliteHttpClient] | None = None,
    progress: Progress | None = None,
    now: datetime | None = None,
) -> tuple[list[SourceCheck], str | None]:
    """Check all candidates concurrently (one client per task keeps per-host pacing simple).

    Returns (checks, search_country) where search_country may be widened to None by the news probe.
    """
    now = now or datetime.now(UTC).replace(tzinfo=None)
    say = progress or (lambda _m: None)

    def make() -> PoliteHttpClient:
        if http_factory is not None:
            return http_factory()
        return PoliteHttpClient(settings, per_host_delay=1.0, timeout=12.0, max_retries=1, backoff_base=1.0)

    tasks: list[tuple[str, Callable[[], Any]]] = []
    seen: set[str] = set()

    def once(url: str) -> bool:
        key = canonical_url(url)
        if key in seen:
            return False
        seen.add(key)
        return True

    def feed_task(url: str, origin: str) -> SourceCheck:
        return check_feed(make(), url, now, origin)

    def site_task(url: str, origin: str, entity: str | None) -> SourceCheck:
        return check_site(make(), url, now, origin, entity)

    def page_task(url: str, origin: str, entity: str, kind: str) -> SourceCheck:
        return check_page(make(), url, origin, entity, kind)

    for url, origin in cands.feeds:
        if once(url):
            tasks.append((f"feed {url}", functools.partial(feed_task, url, origin)))
    for url, origin, entity in cands.sites:
        if once(url):
            tasks.append((f"site {url}", functools.partial(site_task, url, origin, entity)))
    for url, origin, entity, kind in cands.pages:
        if once(url):
            tasks.append((f"page {url}", functools.partial(page_task, url, origin, entity, kind)))

    checks: list[SourceCheck] = []
    search_country = country
    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        news_future = (
            pool.submit(probe_news_search, make(), news_queries, country, language, lookback_days)
            if news_queries
            else None
        )
        futures = {pool.submit(fn): label for label, fn in tasks}
        if tasks:
            say(f"Checking {len(tasks)} feeds, sites and pages (robots.txt respected)...")
        for fut in cf.as_completed(futures):
            label = futures[fut]
            try:
                checks.append(fut.result())
            except Exception as exc:  # a bug in one check must not lose the others
                log.exception("source check failed for %s", label)
                kind, _, value = label.partition(" ")
                checks.append(SourceCheck(kind, value, "dropped", f"check failed ({type(exc).__name__})"))  # type: ignore[arg-type]
        if news_future is not None:
            say("Probing the news search (GDELT allows one request every few seconds)...")
            probe = news_future.result()
            checks.append(probe.check)
            search_country = probe.country
    if cands.subreddits:
        say("Checking subreddits...")
        checks.extend(check_subreddits(cands.subreddits, settings))
    order = {"news_search": 0, "feed": 1, "site": 2, "page": 3, "subreddit": 4}
    checks.sort(key=lambda c: (order[c.kind], c.status != "ok", c.value))
    return checks, search_country
