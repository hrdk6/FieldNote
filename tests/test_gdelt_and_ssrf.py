"""GDELT news search helpers, the news collector's search path, host pacing and the SSRF guard."""

from __future__ import annotations

import json
import socket
from datetime import datetime
from typing import Any

import pytest
import requests

from fieldnote.collectors import base as http_base
from fieldnote.collectors.base import PoliteHttpClient, UnsafeUrlError
from fieldnote.collectors.gdelt import (
    GDELT_DOC_API,
    GdeltError,
    build_query,
    gdelt_term,
    mentions_any,
    parse_response,
    parse_seendate,
    search_params,
)
from fieldnote.collectors.rss_news import RssNewsCollector
from fieldnote.config import get_settings
from tests.test_collectors import FakeResponse, FakeSession, _client

ARTICLE = "<html><body><article><h1>Latest report</h1><p>{}</p></article></body></html>"


def _gdelt_body(*articles: dict[str, Any]) -> str:
    return json.dumps({"articles": list(articles)})


def test_gdelt_terms_and_query() -> None:
    assert gdelt_term("EV") is None  # GDELT rejects words under 3 characters
    assert gdelt_term("Zepto") == "Zepto"
    assert gdelt_term("Swiggy Instamart") == '"Swiggy Instamart"'
    assert gdelt_term("solid-state battery") == '"solid-state battery"'
    assert build_query(["EV", "quick commerce", "Zepto", "zepto"], "india", "english") == (
        '("quick commerce" OR Zepto) sourcecountry:india sourcelang:english'
    )
    assert build_query(["Zepto"]) == "Zepto"
    assert build_query(["EV", "a"]) is None
    many = [f"term{i:02d}" for i in range(30)]
    assert build_query(many).count(" OR ") == 9  # at most 10 terms


def test_gdelt_params_and_parsing() -> None:
    p = search_params("x", 200, 999)
    assert p["timespan"] == "90d" and p["maxrecords"] == "250" and p["format"] == "json"
    assert parse_response("") == []
    assert parse_response('{"articles": [{"url": "https://a.example/1"}, {"title": "no url"}]}') == [
        {"url": "https://a.example/1"}
    ]
    with pytest.raises(GdeltError, match="too short"):
        parse_response("Your search contained a keyword that was too short.\n")
    assert parse_seendate("20260922T110000Z") == datetime(2026, 9, 22, 11, 0, 0)
    assert parse_seendate("garbage") is None
    assert mentions_any("Zepto raises prices", ["zepto"]) and not mentions_any("Zeptosecond physics", ["Zepto"])


def test_config_derives_gdelt_filters(cfg_ev) -> None:  # type: ignore[no-untyped-def]
    assert cfg_ev.gdelt_country() == "india" and cfg_ev.gdelt_language() == "english"
    cfg_ev.sources.news.search_country = "worldwide"
    assert cfg_ev.gdelt_country() is None


def test_news_collector_search_keeps_only_relevant_hits(cfg_ev) -> None:  # type: ignore[no-untyped-def]
    cfg_ev.sources.news.google_news_queries = []
    cfg_ev.sources.news.rss_feeds = []
    cfg_ev.sources.news.search_queries = ["electric scooter"]
    name = cfg_ev.competitors[0].name
    body = _gdelt_body(
        {"url": "https://pub.example/a", "title": f"{name} cuts prices", "seendate": "20260921T090000Z",
         "domain": "pub.example", "sourcecountry": "India", "language": "English"},
        {"url": "https://pub.example/b", "title": "Cricket scores", "seendate": "20260921T090000Z"},
        {"url": "https://pub.example/old", "title": f"{name} old story", "seendate": "20260101T000000Z"},
    )  # fmt: skip
    http, sess = _client(
        {
            "https://api.gdeltproject.org/robots.txt": FakeResponse(404),
            GDELT_DOC_API: FakeResponse(200, body, {"Content-Type": "application/json"}),
            "https://pub.example/robots.txt": FakeResponse(404),
            "https://pub.example/a": FakeResponse(200, ARTICLE.format(f"{name} announced a price cut today.")),
            "https://pub.example/b": FakeResponse(200, ARTICLE.format("Match report and scores.")),
        }
    )
    res = RssNewsCollector(get_settings(), http, now=datetime(2026, 9, 22)).collect(cfg_ev, datetime(2026, 9, 15))
    assert res.status == "ok", res.message
    assert [d.url for d in res.documents] == ["https://pub.example/a"]
    doc = res.documents[0]
    assert doc.metadata["via"] == "gdelt" and name in doc.metadata["entities"]
    assert res.stats["search_hits"] == 3 and res.stats["search_kept"] == 1 and res.stats["search_irrelevant"] == 1
    gdelt_call = next(c for c in sess.calls if c[0] == GDELT_DOC_API)
    assert gdelt_call is not None


def test_news_collector_reports_gdelt_errors(cfg_ev) -> None:  # type: ignore[no-untyped-def]
    cfg_ev.sources.news.google_news_queries = []
    cfg_ev.sources.news.rss_feeds = []
    cfg_ev.sources.news.search_queries = ["electric scooter"]
    http, _ = _client(
        {
            "https://api.gdeltproject.org/robots.txt": FakeResponse(404),
            GDELT_DOC_API: FakeResponse(200, "Your search contained a keyword that was too short.\n"),
        }
    )
    res = RssNewsCollector(get_settings(), http, now=datetime(2026, 9, 22)).collect(cfg_ev, datetime(2026, 9, 15))
    assert res.status == "failed" and "too short" in res.message


def test_gdelt_host_gets_slower_pacing_and_retry_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    http, _ = _client({}, delay=0.0)
    assert http._min_delay("api.gdeltproject.org") == 6.0 and http._min_delay("news.example") == 0.0
    slept: list[float] = []
    monkeypatch.setattr(http_base.time, "sleep", lambda s: slept.append(s))
    http._sleep_backoff(0, None, "api.gdeltproject.org")
    assert slept[-1] >= 20.0


# ---- SSRF guard --------------------------------------------------------------------------------


@pytest.fixture
def guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIELDNOTE_ALLOW_PRIVATE_URLS", "0")


def _guarded(resolver: Any, routes: dict[str, Any] | None = None) -> tuple[PoliteHttpClient, FakeSession]:
    sess = FakeSession(routes or {})
    client = PoliteHttpClient(get_settings(), per_host_delay=0, max_retries=0, session=sess, resolver=resolver)  # type: ignore[arg-type]
    return client, sess


def _resolves_to(ip: str) -> Any:
    def resolver(host: str, _port: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return resolver


@pytest.mark.parametrize(
    "url,ip",
    [
        ("http://169.254.169.254/latest/meta-data/", None),
        ("http://127.0.0.1:8080/admin", None),
        ("http://[::1]/", None),
        ("http://localhost/", None),
        ("http://printer.local/", None),
        ("http://intranet.example/", "10.0.0.5"),
        ("http://sneaky.example/", "192.168.1.10"),
        ("file:///etc/passwd", None),
    ],
)
def test_private_and_non_http_urls_are_refused(guard: None, url: str, ip: str | None) -> None:
    client, sess = _guarded(_resolves_to(ip or "93.184.216.34"))
    with pytest.raises(UnsafeUrlError):
        client.get(url)
    assert sess.calls == []  # nothing was requested, not even robots.txt


def test_public_urls_are_allowed_and_redirects_are_rechecked(guard: None) -> None:
    redirect = FakeResponse(302, "", {"Location": "http://10.0.0.7/internal"}, url="https://pub.example/r")
    redirect.is_redirect = True  # type: ignore[attr-defined]

    def resolver(host: str, _port: Any) -> list[Any]:
        ip = "10.0.0.7" if host == "10.0.0.7" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    client, sess = _guarded(
        resolver,
        {
            "https://pub.example/robots.txt": FakeResponse(404),
            "https://pub.example/ok": FakeResponse(200, "<html>fine</html>"),
            "https://pub.example/r": redirect,
        },
    )
    assert client.get("https://pub.example/ok").ok
    with pytest.raises(UnsafeUrlError):
        client.get("https://pub.example/r")
    assert all("10.0.0.7" not in c[0] for c in sess.calls)


def test_allow_private_urls_setting_disables_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIELDNOTE_ALLOW_PRIVATE_URLS", "1")
    client = PoliteHttpClient(get_settings(), session=FakeSession({}), resolver=_resolves_to("10.0.0.1"))  # type: ignore[arg-type]
    client.check_public("http://intranet.example/")  # no exception


def test_unresolvable_hosts_are_not_treated_as_internal(guard: None) -> None:
    def fail(host: str, _port: Any) -> list[Any]:
        raise socket.gaierror("no such host")

    client, _ = _guarded(fail)
    client.check_public("https://does-not-exist.example/")
    assert isinstance(UnsafeUrlError("x"), requests.RequestException)


def test_feed_relevance_filter_is_opt_in(cfg_ev) -> None:  # type: ignore[no-untyped-def]
    name = cfg_ev.competitors[0].name
    feed = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>{name} opens new stores</title><link>https://feed.example/a</link>
<pubDate>Mon, 21 Sep 2026 10:00:00 GMT</pubDate><description>Expansion news.</description></item>
<item><title>Cricket final preview</title><link>https://feed.example/b</link>
<pubDate>Mon, 21 Sep 2026 11:00:00 GMT</pubDate><description>Sports.</description></item>
</channel></rss>"""
    cfg_ev.sources.news.google_news_queries = []
    cfg_ev.sources.news.search_queries = []
    cfg_ev.sources.news.rss_feeds = ["https://feed.example/rss"]
    cfg_ev.sources.news.fetch_full_text = False
    routes = {
        "https://feed.example/robots.txt": FakeResponse(404),
        "https://feed.example/rss": FakeResponse(200, feed, {"Content-Type": "application/rss+xml"}),
    }

    def collect() -> list[str]:
        http, _ = _client(routes)
        res = RssNewsCollector(get_settings(), http, now=datetime(2026, 9, 22)).collect(cfg_ev, datetime(2026, 9, 15))
        return sorted(d.url for d in res.documents)

    assert collect() == ["https://feed.example/a", "https://feed.example/b"]  # default: keep everything
    cfg_ev.sources.news.filter_feeds_by_relevance = True
    assert collect() == ["https://feed.example/a"]
