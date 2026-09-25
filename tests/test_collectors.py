from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from fieldnote.collectors.base import PoliteHttpClient, RobotsDisallowedError
from fieldnote.collectors.fixtures import FixtureDataset, fixture_collectors
from fieldnote.collectors.reddit import RedditCollector, submission_to_document
from fieldnote.collectors.rss_news import RssNewsCollector, google_news_url
from fieldnote.collectors.youtube import YoutubeCollector
from fieldnote.config import get_settings

ROBOTS = "User-agent: *\nDisallow: /private\n\nUser-agent: FieldNoteBot\nDisallow: /nobots\n"
RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Brand X cuts prices on its city scooter</title><link>https://news.example/a</link>
<pubDate>Mon, 21 Sep 2026 10:00:00 GMT</pubDate><description>Brand X cut prices.</description></item>
<item><title>Old story</title><link>https://news.example/old</link><pubDate>Mon, 01 Jun 2026 10:00:00 GMT</pubDate></item>
</channel></rss>"""


class FakeResponse:
    def __init__(self, status: int, text: str = "", headers: dict[str, str] | None = None, url: str = "") -> None:
        self.status_code = status
        self.text = text
        self.content = text.encode()
        self.headers = headers or {"Content-Type": "text/html"}
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"
        self.url = url


class FakeSession:
    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.times: list[float] = []

    def get(self, url: str, headers: dict[str, str] | None = None, **_: Any) -> FakeResponse:
        self.calls.append((url, headers or {}))
        self.times.append(time.monotonic())
        handler = self.routes.get(url.split("?")[0])
        if handler is None:
            return FakeResponse(404, url=url)
        return handler(headers or {}) if callable(handler) else handler


def _client(routes: dict[str, Any], db: Any = None, delay: float = 0.0) -> tuple[PoliteHttpClient, FakeSession]:
    sess = FakeSession(routes)
    return PoliteHttpClient(
        get_settings(), per_host_delay=delay, timeout=5, max_retries=1, backoff_base=0.01, db=db, session=sess
    ), sess  # type: ignore[arg-type]


def test_robots_txt_is_enforced() -> None:
    # Per RFC 9309 the most specific user-agent group applies: FieldNoteBot's own group here.
    http, sess = _client(
        {
            "https://site.example/robots.txt": FakeResponse(200, ROBOTS),
            "https://site.example/ok": FakeResponse(200, "hi"),
            "https://other.example/robots.txt": FakeResponse(200, "User-agent: *\nDisallow: /private\n"),
        }
    )
    assert http.get("https://site.example/ok").text == "hi"
    with pytest.raises(RobotsDisallowedError):
        http.get("https://site.example/nobots")
    with pytest.raises(RobotsDisallowedError):
        http.get("https://other.example/private/page")
    assert http.stats["robots_blocked"] == 2
    assert sess.calls[0][0].endswith("/robots.txt")
    assert sess.headers["User-Agent"].startswith("FieldNoteBot/1.0 (+")


def test_robots_403_means_disallow_and_404_means_allow() -> None:
    http, _ = _client(
        {"https://a.example/robots.txt": FakeResponse(403), "https://b.example/x": FakeResponse(200, "ok")}
    )
    assert not http.allowed("https://a.example/anything")
    assert http.allowed("https://b.example/x")


def test_per_host_rate_limit() -> None:
    http, sess = _client(
        {"https://r.example/robots.txt": FakeResponse(404), "https://r.example/p": FakeResponse(200, "x")}, delay=0.2
    )
    http.get("https://r.example/p", check_robots=True)
    http.get("https://r.example/p")
    gaps = [b - a for a, b in zip(sess.times, sess.times[1:], strict=False)]
    assert all(g >= 0.18 for g in gaps)


def test_conditional_requests_use_etag(db) -> None:  # type: ignore[no-untyped-def]
    def page(headers: dict[str, str]) -> FakeResponse:
        if headers.get("If-None-Match") == '"v1"':
            return FakeResponse(304)
        return FakeResponse(200, "body", {"Content-Type": "text/html", "ETag": '"v1"'})

    http, sess = _client({"https://c.example/robots.txt": FakeResponse(404), "https://c.example/p": page}, db=db)
    first = http.get("https://c.example/p")
    second = http.get("https://c.example/p", use_cached_body_on_304=True)
    assert first.status == 200 and second.not_modified and second.text == "body"
    assert sess.calls[-1][1]["If-None-Match"] == '"v1"'


def test_retries_then_succeeds() -> None:
    attempts = {"n": 0}

    def flaky(_: dict[str, str]) -> FakeResponse:
        attempts["n"] += 1
        return FakeResponse(503) if attempts["n"] == 1 else FakeResponse(200, "ok")

    http, _ = _client({"https://f.example/robots.txt": FakeResponse(404), "https://f.example/p": flaky})
    assert http.get("https://f.example/p").text == "ok" and attempts["n"] == 2


def test_rss_collector_parses_filters_and_dedupes(cfg_ev) -> None:  # type: ignore[no-untyped-def]
    cfg_ev.sources.news.google_news_queries = []
    cfg_ev.sources.news.search_queries = []
    cfg_ev.sources.news.rss_feeds = ["https://feed.example/rss"]
    cfg_ev.sources.news.fetch_full_text = False
    http, _ = _client(
        {
            "https://feed.example/robots.txt": FakeResponse(404),
            "https://feed.example/rss": FakeResponse(200, RSS, {"Content-Type": "application/rss+xml"}),
        }
    )
    res = RssNewsCollector(get_settings(), http, now=datetime(2026, 9, 22)).collect(cfg_ev, datetime(2026, 9, 15))
    assert res.status == "ok"
    assert [d.url for d in res.documents] == ["https://news.example/a"]


def test_google_news_url_is_region_aware() -> None:
    url = google_news_url("electric scooter", "en-IN", "IN", "IN:en", 7)
    assert "hl=en-IN" in url and "gl=IN" in url and "ceid=IN:en" in url and "when%3A7d" in url


def test_social_collectors_skip_without_credentials(cfg_ev) -> None:  # type: ignore[no-untyped-def]
    for cls in (RedditCollector, YoutubeCollector):
        res = cls(get_settings(), None).collect(cfg_ev, datetime(2026, 9, 1))
        assert res.status == "skipped" and "not set" in res.message


def test_reddit_documents_never_store_authors() -> None:
    sub = SimpleNamespace(
        id="abc",
        title="Range report",
        selftext="My scooter gives 90 km range.",
        permalink="/r/x/comments/abc/",
        created_utc=datetime(2026, 9, 20, tzinfo=UTC).timestamp(),
        score=5,
        num_comments=2,
        subreddit=SimpleNamespace(display_name="x"),
        author=SimpleNamespace(name="some_user_123"),
    )
    doc = submission_to_document(sub, {}, datetime(2026, 9, 22))
    blob = repr(doc.__dict__)
    assert "some_user_123" not in blob and "author" not in doc.metadata
    assert doc.url == "https://www.reddit.com/r/x/comments/abc/"


def test_fixture_collectors_replay_rounds(cfg_ev) -> None:  # type: ignore[no-untyped-def]
    ds = FixtureDataset(cfg_ev.workspace)
    assert [r.label for r in ds.rounds] == ["previous", "current"]
    now = ds.rounds[1].as_of
    docs = []
    for c in fixture_collectors(get_settings(), cfg_ev.workspace, "current", now):
        docs += c.collect(cfg_ev, ds.rounds[0].as_of).documents
    assert all(d.metadata.get("fixture") for d in docs)
    assert all(".example" in d.url for d in docs)
    assert any(d.metadata.get("injection_suspect") for d in docs)
