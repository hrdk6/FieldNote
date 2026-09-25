"""HTML cleaning and main-text extraction.

All collected content is treated as untrusted: scripts, styles, comments and visually hidden
elements are removed (hidden text is a common prompt-injection vector), and the result is stored as
plain text.
"""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup, Comment

from fieldnote.textutil import extract_numbers, injection_score, normalize_ws

_HIDDEN_STYLE = re.compile(
    r"(?i)(display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0|opacity\s*:\s*0(?:\.0+)?\s*(?:;|$))"
)
_DROP_TAGS = ("script", "style", "noscript", "template", "iframe", "svg", "canvas", "form", "button", "input", "select")
_BOILERPLATE_TAGS = ("nav", "footer", "aside")


def _soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # pragma: no cover - lxml missing
        return BeautifulSoup(html, "html.parser")


def strip_untrusted_html(html: str, *, drop_boilerplate: bool = True) -> BeautifulSoup:
    soup = _soup(html or "")
    for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
        c.extract()
    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()
    if drop_boilerplate:
        for tag in soup.find_all(_BOILERPLATE_TAGS):
            tag.decompose()
        for tag in soup.find_all(True):
            if tag.attrs is None:
                continue
            marker = " ".join([*(tag.get("class") or []), str(tag.get("id") or "")]).lower()
            if any(k in marker for k in ("cookie", "consent", "gdpr", "newsletter-popup")):
                tag.decompose()
    for tag in soup.find_all(True):
        if tag.attrs is None:
            continue
        style = tag.get("style") or ""
        if (
            tag.has_attr("hidden")
            or tag.get("aria-hidden") == "true"
            or _HIDDEN_STYLE.search(str(style))
            or any(c in ("sr-only", "visually-hidden", "hidden", "d-none") for c in (tag.get("class") or []))
        ):
            tag.decompose()
    return soup


def html_to_text(html: str) -> str:
    soup = strip_untrusted_html(html)
    lines = []
    for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "td", "th", "span", "div"]):
        if el.find(["p", "li", "div", "h1", "h2", "h3", "h4", "table"]):
            continue
        t = normalize_ws(el.get_text(" "))
        if t:
            lines.append(t)
    if not lines:
        return normalize_ws(soup.get_text(" "))
    # Preserve order but drop exact duplicate lines.
    seen: set[str] = set()
    out = []
    for line in lines:
        if line not in seen:
            seen.add(line)
            out.append(line)
    return "\n".join(out)


def extract_main_text(html: str, url: str | None = None) -> str:
    """trafilatura main-content extraction on sanitised HTML, falling back to a structural walk."""
    soup = strip_untrusted_html(html, drop_boilerplate=True)
    cleaned_html = str(soup)
    text = ""
    try:
        import trafilatura

        # deduplicate=False: trafilatura's dedup cache is process-wide, which would make repeated extraction of
        # the same page non-deterministic (and create phantom page changes).
        text = (
            trafilatura.extract(
                cleaned_html, url=url, include_comments=False, include_tables=True, favor_recall=True, deduplicate=False
            )
            or ""
        )
    except Exception:
        text = ""
    fallback = html_to_text(html)
    # trafilatura can drop short pricing/product pages entirely; prefer the richer text.
    if len(text) < 0.5 * len(fallback):
        text = fallback
    return text.strip()


_PRICE_LINE = re.compile(r"(₹|Rs\.?|INR|\$|USD|€|£)\s?\d")


def structured_hints(html: str, text: str, selector: str | None = None) -> dict[str, Any]:
    """Prices (currency regex, optionally scoped by a CSS selector) and headings."""
    soup = strip_untrusted_html(html, drop_boilerplate=False)
    scope_text = text
    if selector:
        nodes: Any
        try:
            nodes = soup.select(selector)
        except Exception:
            nodes = []
        if nodes:
            scope_text = "\n".join(normalize_ws(n.get_text(" ")) for n in nodes)
    prices = []
    for line in scope_text.splitlines():
        if not _PRICE_LINE.search(line):
            continue
        for n in extract_numbers(line):
            if n.has_currency and n.value >= 1:
                label = normalize_ws(line[: max(0, n.start)]).strip(" :-–|")
                prices.append({"label": label[-80:], "value": n.value, "raw": n.raw})
    headings = [normalize_ws(h.get_text(" ")) for h in soup.find_all(["h1", "h2", "h3"])]
    headings = [h for h in headings if h][:50]
    return {"prices": prices[:50], "headings": headings}


def sanitize_text(text: str) -> tuple[str, dict[str, Any]]:
    """Normalise plain text (e.g. from APIs) and flag likely prompt-injection content."""
    clean = re.sub(r"<[^>]{1,200}>", " ", text or "")  # stray markup in API text
    clean = re.sub(r"[ \t]+", " ", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    flags: dict[str, Any] = {}
    score = injection_score(clean)
    if score:
        flags["injection_suspect"] = True
        flags["injection_score"] = score
    return clean, flags
