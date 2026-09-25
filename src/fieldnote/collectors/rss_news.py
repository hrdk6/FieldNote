"""News collector: GDELT topic search, configured RSS/Atom feeds, and region-aware Google News RSS queries.

* ``sources.news.search_queries`` -> one combined GDELT DOC API query per run (open API, no key), filtered
  by source country/language, then each article is fetched from its publisher (robots.txt permitting).
  Hits that mention none of the workspace's terms are dropped (GDELT matches article bodies loosely).
* ``sources.news.rss_feeds`` -> feeds parsed with feedparser.
* ``sources.news.google_news_queries`` -> Google News RSS; skipped while news.google.com's robots.txt
  disallows crawlers.
"""

from __future__ import annotations

import calendar
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote_plus

import feedparser
import requests

from fieldnote.collectors.base import Collector, RobotsDisallowedError
from fieldnote.collectors.gdelt import (
    GDELT_DOC_API,
    GdeltError,
    build_query,
    mentions_any,
    parse_response,
    parse_seendate,
    search_params,
)
from fieldnote.config import WorkspaceConfig
from fieldnote.domain import CollectedDocument, CollectorResult
from fieldnote.logging_setup import get_logger
from fieldnote.processing.clean import extract_main_text, sanitize_text
from fieldnote.processing.dedupe import dedupe_documents, strip_publisher_suffix
from fieldnote.textutil import canonical_url, find_entities, host_of, normalize_ws

log = get_logger(__name__)

GOOGLE_NEWS_HOST = "news.google.com"
# Upper bound on GDELT hits fetched per run (each costs one publisher request).
MAX_SEARCH_ARTICLES = 40


def google_news_url(query: str, hl: str, gl: str, ceid: str, lookback_days: int) -> str:
    q = quote_plus(f"{query} when:{lookback_days}d")
    return f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"


def _entry_datetime(entry: feedparser.FeedParserDict) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        tm = entry.get(key)
        if tm:
            return datetime.fromtimestamp(calendar.timegm(tm), UTC).replace(tzinfo=None)
    return None


def relevance_terms(cfg: WorkspaceConfig) -> list[str]:
    """Words that make a search hit relevant: queries, entity names/aliases and keywords."""
    terms = [*cfg.sources.news.search_queries, *cfg.keywords]
    for comp in cfg.competitors:
        terms.extend(comp.all_names())
    return [t for t in dict.fromkeys(t.strip() for t in terms) if len(t) >= 2]


class RssNewsCollector(Collector):
    name = "news"

    def _collect(self, cfg: WorkspaceConfig, since: datetime) -> CollectorResult:
        assert self.http is not None
        news = cfg.sources.news
        loc = cfg.news_locale()
        feeds: list[tuple[str, str, bool]] = []  # (url, source label, is_google)
        for q in news.google_news_queries:
            feeds.append((google_news_url(q, loc.hl, loc.gl, loc.ceid, news.lookback_days), f"Google News: {q}", True))
        for f in news.rss_feeds:
            feeds.append((f, host_of(f), False))
        self._aliases = cfg.entity_aliases()
        self._blocked = 0
        self._full_text = 0
        docs: list[CollectedDocument] = []
        errors: list[str] = []
        cutoff = min(since, self.now - timedelta(days=news.lookback_days))
        search_stats: dict[str, Any] = {}
        if news.search_queries:
            search_stats = self._collect_search(cfg, cutoff, docs, errors)
        terms = relevance_terms(cfg) if news.filter_feeds_by_relevance else []
        feed_irrelevant = 0
        for feed_url, label, is_google in feeds:
            try:
                res = self.http.get(feed_url, conditional=True)
            except RobotsDisallowedError:
                self._blocked += 1
                log.info("robots.txt disallows feed %s; skipped", feed_url)
                continue
            except requests.RequestException as exc:
                errors.append(f"{label}: {type(exc).__name__}")
                continue
            if res.not_modified:
                continue
            if not res.ok:
                errors.append(f"{label}: HTTP {res.status}")
                continue
            parsed = feedparser.parse(res.text)
            count = 0
            for entry in parsed.entries:
                if count >= news.max_items_per_feed or len(docs) >= cfg.limits.max_documents_per_source:
                    break
                published = _entry_datetime(entry)
                if published and published <= cutoff:
                    continue
                link = entry.get("link") or ""
                if not link:
                    continue
                title = normalize_ws(entry.get("title", ""))
                source_name = label
                if is_google:
                    src = entry.get("source") or {}
                    source_name = normalize_ws(getattr(src, "title", None) or src.get("title", "") or label)
                    title = strip_publisher_suffix(title, source_name)
                summary_html = entry.get("summary", "") or ""
                summary = (
                    normalize_ws(extract_main_text(summary_html)) if "<" in summary_html else normalize_ws(summary_html)
                )
                # Cheap pre-check on title + summary before fetching the article (saves a request per item).
                if terms and not mentions_any(f"{title}\n{summary}", terms):
                    feed_irrelevant += 1
                    continue
                # Google News links are JS redirects; only fetch full text for direct publisher links.
                content = self._article_text(link) if news.fetch_full_text and host_of(link) != GOOGLE_NEWS_HOST else ""
                meta: dict[str, Any] = {"feed": feed_url}
                if is_google:
                    meta["via"] = "google_news"
                docs.append(self._document(link, title, summary, content, published, source_name, meta))
                count += 1
        before = len(docs)
        docs = dedupe_documents(docs)
        status = "ok" if not errors else ("partial" if docs else "failed")
        return CollectorResult(
            name=self.name,
            documents=docs,
            status=status,
            message="; ".join(errors[:5]),
            stats={
                "feeds": len(feeds),
                "items": before,
                "after_dedupe": len(docs),
                "robots_blocked": self._blocked,
                "full_text": self._full_text,
                "errors": len(errors),
                "feed_irrelevant": feed_irrelevant,
                **search_stats,
            },
        )

    # ---- GDELT topic search -----------------------------------------------------------------
    def _collect_search(
        self, cfg: WorkspaceConfig, cutoff: datetime, docs: list[CollectedDocument], errors: list[str]
    ) -> dict[str, Any]:
        assert self.http is not None
        news = cfg.sources.news
        query = build_query(news.search_queries, cfg.gdelt_country(), cfg.gdelt_language())
        if query is None:
            errors.append("news search: no usable search terms (terms need 3+ characters)")
            return {"search_hits": 0}
        max_records = min(MAX_SEARCH_ARTICLES, max(news.max_items_per_feed, 10) * 2)
        try:
            res = self.http.get(
                GDELT_DOC_API, conditional=False, params=search_params(query, news.lookback_days, max_records)
            )
        except RobotsDisallowedError:
            self._blocked += 1
            return {"search_hits": 0}
        except requests.RequestException as exc:
            errors.append(f"news search: {type(exc).__name__}")
            return {"search_hits": 0}
        if not res.ok:
            errors.append(f"news search: HTTP {res.status}")
            return {"search_hits": 0}
        try:
            articles = parse_response(res.text)
        except GdeltError as exc:
            errors.append(f"news search: {exc}")
            return {"search_hits": 0}
        terms = relevance_terms(cfg)
        kept = irrelevant = 0
        seen: set[str] = set()
        for art in articles:
            if len(docs) >= cfg.limits.max_documents_per_source:
                break
            link = str(art.get("url", "")).strip()
            key = canonical_url(link)
            if not link.startswith(("http://", "https://")) or key in seen:
                continue
            seen.add(key)
            published = parse_seendate(art.get("seendate"))
            if published and published <= cutoff:
                continue
            title = normalize_ws(str(art.get("title", "")))
            content = self._article_text(link) if news.fetch_full_text else ""
            if not mentions_any(f"{title}\n{content}", terms):
                irrelevant += 1
                continue
            source_name = str(art.get("domain") or host_of(link))
            meta = {
                "via": "gdelt",
                "search_query": query,
                "source_country": art.get("sourcecountry"),
                "source_language": art.get("language"),
            }
            docs.append(self._document(link, title, "", content, published, source_name, meta))
            kept += 1
        return {"search_hits": len(articles), "search_kept": kept, "search_irrelevant": irrelevant}

    # ---- shared helpers ------------------------------------------------------------------------
    def _article_text(self, link: str) -> str:
        assert self.http is not None
        try:
            page = self.http.get(link, conditional=False)
        except RobotsDisallowedError:
            self._blocked += 1
            return ""
        except requests.RequestException as exc:
            log.debug("article fetch failed for %s: %s", link, exc)
            return ""
        if page.ok and page.text:
            self._full_text += 1
            return extract_main_text(page.text, url=link)
        return ""

    def _document(
        self,
        link: str,
        title: str,
        summary: str,
        content: str,
        published: datetime | None,
        source_name: str,
        meta: dict[str, Any],
    ) -> CollectedDocument:
        text, flags = sanitize_text(content or summary or title)
        meta = {**meta, "entities": find_entities(f"{title}\n{text}", self._aliases), **flags}
        return CollectedDocument(
            source_type="news",
            source_name=source_name[:200],
            url=link,
            canonical_url=canonical_url(link),
            title=title,
            summary=summary[:2000],
            content=text,
            published_at=published,
            fetched_at=self.now,
            metadata=meta,
        )
