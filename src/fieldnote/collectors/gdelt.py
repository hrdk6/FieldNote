"""GDELT DOC 2.0 API helpers: topic news search for any market, company set or technology.

GDELT (https://www.gdeltproject.org) is an open research dataset; its DOC API needs no key and asks
clients to send at most one request every 5 seconds. FieldNote sends one combined query per workspace
run (the polite HTTP client spaces requests to that host at 6 seconds and backs off on 429), then
fetches each article from its publisher with the usual robots.txt checks.

Query syntax notes (verified against the live API):
* single words shorter than 3 characters are rejected ("keyword too short");
* multi-word terms must be quoted phrases inside an ``(a OR b)`` group;
* ``sourcecountry:`` takes a country name without spaces (``india``, ``unitedstates``), not a code;
* errors come back as plain text with HTTP 200, so the body is parsed defensively.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
MAX_TERMS = 10
MAX_QUERY_CHARS = 400
MAX_TIMESPAN_DAYS = 90


class GdeltError(Exception):
    """GDELT answered with an error message instead of JSON."""


def gdelt_term(term: str) -> str | None:
    """Normalise one search term to GDELT syntax, or None if GDELT would reject it."""
    clean = " ".join(re.sub(r"[^\w\s&'.+-]", " ", term).split())
    if len(clean) < 3:
        return None
    if re.fullmatch(r"[A-Za-z0-9]+", clean):
        return clean
    return f'"{clean}"'


def build_query(terms: list[str], country: str | None = None, language: str | None = None) -> str | None:
    """Combine terms into one ``(a OR "b c")`` query with optional source country/language filters."""
    parts: list[str] = []
    seen: set[str] = set()
    for t in terms:
        g = gdelt_term(t)
        if g is None or g.lower() in seen:
            continue
        candidate = [*parts, g]
        if len(" OR ".join(candidate)) + 2 > MAX_QUERY_CHARS or len(candidate) > MAX_TERMS:
            break
        parts.append(g)
        seen.add(g.lower())
    if not parts:
        return None
    query = parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"
    if country:
        query += f" sourcecountry:{country}"
    if language:
        query += f" sourcelang:{language}"
    return query


def search_params(query: str, lookback_days: int, max_records: int) -> dict[str, str]:
    return {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": str(max(1, min(max_records, 250))),
        "timespan": f"{max(1, min(lookback_days, MAX_TIMESPAN_DAYS))}d",
        "sort": "datedesc",
    }


def parse_response(text: str) -> list[dict[str, Any]]:
    """Articles from a DOC API response; raises :class:`GdeltError` with GDELT's message on errors."""
    body = (text or "").strip()
    if not body:
        return []
    try:
        data = json.loads(body, strict=False)
    except json.JSONDecodeError as exc:
        raise GdeltError(body.splitlines()[0][:200]) from exc
    if not isinstance(data, dict):
        raise GdeltError("unexpected response shape")
    articles = data.get("articles") or []
    return [a for a in articles if isinstance(a, dict) and a.get("url")]


def parse_seendate(value: str | None) -> datetime | None:
    """GDELT ``seendate`` (``20260922T110000Z``) -> naive UTC datetime."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y%m%dT%H%M%SZ")
    except ValueError:
        return None


def mentions_any(text: str, terms: list[str]) -> bool:
    """Case-insensitive whole-word match of any term (used to drop loosely related search hits)."""
    low = text.lower()
    for t in terms:
        t = t.strip().lower()
        if len(t) >= 2 and re.search(rf"(?<![\w]){re.escape(t)}(?![\w])", low):
            return True
    return False
