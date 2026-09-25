"""Rule-based workspace draft: used offline (MockClient) and as a fallback when the LLM call fails.

It reads a plain-language description such as "Quick commerce in India: Blinkit, Zepto, Swiggy
Instamart" and produces a :class:`~fieldnote.llm.schemas.WorkspaceDraft`-shaped dict. It never
proposes URLs (only the LLM or the user can, and every URL is verified before use).
"""

from __future__ import annotations

import re
from typing import Any

from fieldnote.config import REGION_PROFILES, region_profile

# Adjectives and short forms that imply a region. "us" is excluded on purpose (it is also a pronoun).
_REGION_ALIASES: dict[str, str] = {
    "indian": "India",
    "american": "United States",
    "usa": "United States",
    "u.s.": "United States",
    "british": "United Kingdom",
    "uk": "United Kingdom",
    "german": "Germany",
    "french": "France",
    "japanese": "Japan",
    "chinese": "China",
    "korean": "South Korea",
    "indonesian": "Indonesia",
    "brazilian": "Brazil",
    "canadian": "Canada",
    "australian": "Australia",
    "mexican": "Mexico",
    "nigerian": "Nigeria",
    "kenyan": "Kenya",
    "european": "Europe",
    "europe": "Europe",
    "southeast asia": "Southeast Asia",
    "south east asia": "Southeast Asia",
    "latin america": "Latin America",
    "middle east": "Middle East",
    "africa": "Africa",
    "global": "Global",
    "worldwide": "Global",
    "international": "Global",
}
_REGION_DISPLAY = {k: " ".join(w.capitalize() for w in k.split()) for k in REGION_PROFILES}
_REGION_DISPLAY.update(
    {"usa": "United States", "us": "United States", "america": "United States", "uk": "United Kingdom", "uae": "UAE"}
)
_REGION_KEYS = [(k, v) for k, v in [*_REGION_DISPLAY.items(), *_REGION_ALIASES.items()] if k != "us"]

_LEAD_VERBS = re.compile(
    r"^(?:please\s+)?(?:i\s+(?:want|would like|need)\s+to\s+)?(?:track|monitor|follow|watch|cover|research|"
    r"analy[sz]e|keep an eye on|keep tabs on|stay on top of)\s+",
    re.I,
)
# "such as X, Y": the list starts after the marker.
_LIST_INTRO = re.compile(r"\b(?:such as|like|including|e\.g\.|for example|especially|mainly|namely)\s*:?\s+", re.I)
# "... brands X, Y": the noun belongs to the topic, the list starts after it.
_LIST_NOUN = re.compile(r"\b(?:competitors|players|brands|companies|startups|makers|apps)\b\s*:?\s+", re.I)
# "&" and "/" are not separators: they appear inside names (AT&T, Johnson & Johnson).
_SPLIT = re.compile(r"\s*(?:,|;|\band\b|\bor\b|\bvs\.?|\bversus\b)\s*", re.I)
_LEADING_FUNCTION_WORDS = re.compile(r"^(?:(?:in|of|for|from|across|the|a|an|with|by|at|on)\s+)+", re.I)
_STOP_ENTITY = {
    "the", "a", "an", "others", "other", "etc", "more", "many", "startups", "brands", "companies", "players",
    "competitors", "market", "markets", "industry", "sector", "space", "segment",
}  # fmt: skip

_GENERIC_ASPECTS: dict[str, list[str]] = {
    "price": ["price", "pricing", "cost", "expensive", "cheap", "discount", "offer", "fees"],
    "quality": ["quality", "defect", "broken", "reliable", "reliability", "durable", "issue"],
    "features": ["feature", "features", "spec", "specs", "capability", "option"],
    "customer_service": ["service", "support", "customer care", "helpline", "refund", "complaint"],
    "availability": ["availability", "stock", "waiting", "wait time", "coverage", "launch"],
}
_TOPIC_ASPECTS: list[tuple[tuple[str, ...], dict[str, list[str]]]] = [
    (
        ("delivery", "grocery", "commerce", "food", "e-commerce", "ecommerce", "quick"),
        {
            "delivery_speed": ["delivery", "minutes", "late", "delayed", "fast", "on time"],
            "app_experience": ["app", "checkout", "crash", "ui", "payment failed"],
        },
    ),
    (
        ("battery", "electric", "ev", "evs", "scooter", "car", "cars", "phone", "smartphone", "laptop", "vehicle"),
        {
            "battery_life": ["battery", "range", "charge", "charging", "backup", "drain"],
            "performance": ["performance", "speed", "power", "lag", "smooth", "heating"],
        },
    ),
    (
        ("bank", "banks", "fintech", "payment", "payments", "loan", "card", "insurance", "wallet", "upi"),
        {
            "trust": ["fraud", "scam", "safe", "secure", "trust", "blocked"],
            "app_experience": ["app", "login", "otp", "crash", "kyc"],
        },
    ),
    (
        ("software", "saas", "ai", "cloud", "platform", "tool", "tools", "api", "llm", "assistants"),
        {
            "reliability": ["outage", "downtime", "bug", "bugs", "reliable", "error"],
            "integrations": ["integration", "integrations", "api", "plugin", "connect"],
        },
    ),
]


_FUNCTION_WORDS = {"in", "of", "for", "from", "across", "the", "a", "an", "and", "or", "to", "with", "by", "at", "on"}


def region_terms(region: str) -> list[str]:
    """Lowercase words and phrases that name ``region`` ("india", "indian"), longest first."""
    target = " ".join(region.lower().split())
    terms = {k for k, v in _REGION_KEYS if v.lower() == target} | ({target} if target else set())
    return sorted(terms, key=len, reverse=True)


def strip_region_terms(query: str, terms: list[str], protected: set[str]) -> str:
    """Drop a leading/trailing region word from a search phrase ("Blinkit India" -> "Blinkit").

    Names are never changed ("Bank of India" stays: the remainder would end in a function word), nor
    are phrases that exactly match a protected entity name or alias.
    """
    q = " ".join(query.split())
    if not terms or q.lower() in protected:
        return q
    out = q
    for t in terms:
        esc = re.escape(t)
        # Trailing ("Blinkit India", "apps in the United States"): keep at least 4 characters ("Air India").
        cand = re.sub(rf"\s+(?:(?:in|across|for|from)\s+)?(?:the\s+)?{esc}$", "", out, flags=re.I).strip()
        if cand != out and len(cand) >= 4:
            out = cand
        # Leading ("Indian EV market"): keep at least two words ("Indian Oil" stays).
        cand = re.sub(rf"^(?:the\s+)?{esc}\s+", "", out, flags=re.I).strip()
        if cand != out and len(cand.split()) >= 2:
            out = cand
    words = out.lower().split()
    if not words or words[-1] in _FUNCTION_WORDS or words[0] in _FUNCTION_WORDS:
        return q
    return out


def detect_region(text: str) -> str:
    """First region mentioned in the text (country, adjective or area), else "Global"."""
    low = f" {text.lower()} "
    best: tuple[int, str] | None = None
    for key, display in _REGION_KEYS:
        m = re.search(rf"(?<![\w.]){re.escape(key)}(?![\w])", low)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), display)
    return best[1] if best else "Global"


def _strip_region(text: str, region: str) -> str:
    out = text
    keys = sorted((k for k, v in _REGION_KEYS if v == region), key=len, reverse=True)
    for k in keys:
        out = re.sub(rf"\s*\b(?:in|across|for|from)?\s*{re.escape(k)}(?![\w])", " ", out, flags=re.I)
    return " ".join(out.split())


def _clean_entity(raw: str) -> str:
    s = re.sub(r"\(.*?\)", " ", raw)
    # Keep apostrophes that belong to a name ("Dunkin'", "L'Oréal"); drop surrounding quotes only.
    s = " ".join(s.strip(' .:;-"').split())
    if len(s) > 1 and s[0] == s[-1] == "'":
        s = s[1:-1].strip()
    s = _LEADING_FUNCTION_WORDS.sub("", s)
    # "Samsung in India" -> "Samsung": drop a trailing region phrase.
    for key, _display in sorted(_REGION_KEYS, key=lambda kv: len(kv[0]), reverse=True):
        s = re.sub(rf"\s+(?:in|across|for|from)\s+{re.escape(key)}$", "", s, flags=re.I)
    return s.strip(" .:;-")


_REGION_WORDS = {k for k, _ in _REGION_KEYS} | {v.lower() for _, v in _REGION_KEYS}


def _looks_like_name(s: str, *, in_list: bool = False) -> bool:
    """Plausible company/brand/product name.

    A bare region ("India") is never a name, but names containing one are ("Air India", "Amazon UK",
    "British Airways"). In an explicit list ("...: blinkit, cult.fit") lowercase brands are accepted;
    elsewhere a name needs a capital letter or digit.
    """
    if not s or len(s) < 2 or len(s) > 60 or len(s.split()) > 5:
        return False
    low = s.lower()
    if low in _STOP_ENTITY or low in _REGION_WORDS:
        return False
    if in_list:
        return any(ch.isalnum() for ch in s)
    return any(ch.isupper() for ch in s) or any(ch.isdigit() for ch in s)


def split_description(description: str) -> tuple[str, list[str]]:
    """(topic phrase, explicit entity names) from a description."""
    text = " ".join(description.replace("\n", " ").split())
    text = re.sub(r"https?://\S+", " ", text)
    text = _LEAD_VERBS.sub("", text).strip()
    topic_part, list_part = text, ""
    explicit = ":" in text
    if explicit:
        topic_part, list_part = text.split(":", 1)
    elif m := _LIST_INTRO.search(text):
        topic_part, list_part = text[: m.start()], text[m.end() :]
    elif m := _LIST_NOUN.search(text):
        topic_part, list_part = text[: m.end()], text[m.end() :]
    # The list of names ends with its sentence ("Blinkit, Zepto. See ..." -> "Blinkit, Zepto").
    # Two word characters before the dot, so initials such as "e.l.f. Cosmetics" are not a sentence end.
    list_part = re.split(r"(?<=\w\w)\.\s+(?=[A-Z])", list_part, maxsplit=1)[0]
    entities: list[str] = []
    for chunk in _SPLIT.split(list_part):
        name = _clean_entity(chunk)
        if _looks_like_name(name, in_list=explicit) and name.lower() not in {e.lower() for e in entities}:
            entities.append(name)
    return " ".join(topic_part.strip(" .,:;").split()), entities


def _proper_noun_phrases(text: str) -> list[str]:
    """Runs of capitalised words that are not sentence-initial (fallback entity finder)."""
    tokens = re.findall(r"[A-Za-z0-9][\w&'.+-]*", text)
    out: list[str] = []
    cur: list[str] = []
    for i, tok in enumerate(tokens):
        if i > 0 and any(c.isupper() for c in tok):
            cur.append(tok)
            continue
        if cur:
            out.append(" ".join(cur))
        cur = []
    if cur:
        out.append(" ".join(cur))
    return [p for p in out if _looks_like_name(p)]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        key = it.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(it.strip())
    return out


def heuristic_draft(payload: dict[str, Any]) -> dict[str, Any]:
    description = str(payload.get("description", "")).strip()
    region = str(payload.get("region") or "").strip() or detect_region(description)
    topic, entities = split_description(description)
    topic = _strip_region(topic, region) or topic
    if not entities:
        entities = _proper_noun_phrases(description)
    focal = str(payload.get("focal_company") or "").strip() or None
    if focal and focal.lower() not in {e.lower() for e in entities}:
        entities.insert(0, focal)
    topic_clean = topic.strip() or (entities[0] if entities else "Market")
    if not entities:
        entities = [topic_clean[:60]]
    entities = _dedupe(entities)[:12]
    low = f" {description.lower()} "
    aspects: dict[str, list[str]] = dict(_GENERIC_ASPECTS)
    for triggers, extra in _TOPIC_ASPECTS:
        if any(re.search(rf"(?<![\w]){re.escape(t)}(?![\w])", low) for t in triggers):
            for k, v in extra.items():
                aspects.setdefault(k, v)
    aspect_list = [{"name": k, "keywords": v} for k, v in list(aspects.items())[:8]]
    topic_query = topic_clean if 3 <= len(topic_clean) <= 60 else ""
    news_queries = [q for q in _dedupe([topic_query, *entities]) if len(q) >= 3][:6]
    prof = region_profile(region)
    lang = str(payload.get("language") or "").strip().lower()
    languages = [lang] if lang else (["en", "hi-en"] if region == "India" else ["en"])
    perspective = str(payload.get("perspective") or "").strip() or (
        f"{focal} strategy team" if focal else "market-level observer"
    )
    display_topic = topic_clean[:1].upper() + topic_clean[1:]
    display = display_topic if region == "Global" else f"{display_topic}, {region}"
    return {
        "display_name": display[:80],
        "sector": topic_clean.lower()[:120],
        "region": region,
        "timezone": prof[1] if prof else "UTC",
        "language_hints": languages,
        "perspective": perspective[:200],
        "focal_company": focal,
        "entities": [{"name": e, "aliases": [], "kind": "company", "website": "", "pages": []} for e in entities],
        "keywords": [topic_clean.lower()] if topic_clean else [],
        "aspects": aspect_list,
        "news_queries": news_queries,
        "feed_urls": [],
        "publication_sites": [],
        "subreddits": [],
        "reddit_search_terms": _dedupe([*entities[:4], topic_clean])[:6],
        "youtube_search_terms": _dedupe([f"{topic_clean} review", *[f"{e} review" for e in entities[:2]]])[:3],
        "claim_vs_reported": [],
    }
