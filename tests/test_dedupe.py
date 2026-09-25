from __future__ import annotations

from fieldnote.domain import CollectedDocument
from fieldnote.processing.dedupe import dedupe_documents, strip_publisher_suffix, titles_match


def _doc(url: str, title: str, content: str = "", source: str = "Outlet") -> CollectedDocument:
    return CollectedDocument(source_type="news", source_name=source, url=url, title=title, content=content)


def test_exact_url_and_tracking_params_collapse() -> None:
    docs = [
        _doc("https://a.example/story?utm_source=x", "Brand cuts prices across range", "short"),
        _doc("https://a.example/story", "Brand cuts prices across range", "a much longer body of text"),
    ]
    out = dedupe_documents(docs)
    assert len(out) == 1
    assert out[0].content == "a much longer body of text"


def test_syndicated_stories_collapse_with_sources_noted() -> None:
    docs = [
        _doc(
            "https://one.example/x",
            "Voltra cuts S2 Pro price by ₹10,000 ahead of festive season",
            "full article text here",
            "One",
        ),
        _doc("https://two.example/y", "Voltra cuts S2 Pro price by Rs 10,000 ahead of festive season - Two", "", "Two"),
    ]
    out = dedupe_documents(docs)
    assert len(out) == 1
    assert out[0].metadata["syndicated_sources"] == ["Two"]
    assert "https://two.example/y" in out[0].metadata["also_seen_at"]


def test_distinct_stories_are_kept() -> None:
    docs = [
        _doc("https://one.example/x", "Voltra cuts S2 Pro price by ₹10,000 ahead of festive season"),
        _doc("https://two.example/y", "Zipp launches Z3 Lite for first-time riders in thirty cities"),
    ]
    assert len(dedupe_documents(docs)) == 2


def test_content_hash_duplicates_collapse() -> None:
    docs = [
        _doc("https://a.example/1", "Same", "identical body"),
        _doc("https://b.example/2", "Same", "identical body"),
    ]
    assert len(dedupe_documents(docs)) == 1


def test_title_helpers() -> None:
    assert (
        strip_publisher_suffix("Big launch for scooters this week - Some Publisher", "Some Publisher")
        == "Big launch for scooters this week"
    )
    assert titles_match("Brand X unveils a new scooter for city riders", "Brand X unveils new scooter for city riders")
    assert not titles_match("short", "short")
