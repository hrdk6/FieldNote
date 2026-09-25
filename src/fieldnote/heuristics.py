"""Deterministic, sector-neutral heuristics.

These power the MockClient (offline demo and tests) and act as graceful-degradation fallbacks when
an LLM call fails. They contain no sector knowledge: aspects, aliases and metric vocabulary always
come from the workspace config.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from fieldnote.textutil import (
    extract_numbers,
    find_entities,
    looks_like_injection,
    normalize_ws,
    split_sentences,
    tokenize,
    truncate_words,
)

# ---------------------------------------------------------------------------------------------
# Sentiment (English + common Hinglish)
# ---------------------------------------------------------------------------------------------

POSITIVE = {
    "good",
    "great",
    "excellent",
    "amazing",
    "awesome",
    "love",
    "loved",
    "loving",
    "happy",
    "satisfied",
    "smooth",
    "reliable",
    "impressive",
    "best",
    "fantastic",
    "solid",
    "worth",
    "recommend",
    "recommended",
    "quick",
    "fast",
    "helpful",
    "polite",
    "value",
    "perfect",
    "nice",
    "superb",
    "fine",
    "improved",
    "comfortable",
    "stable",
    "premium",
    "brilliant",
    "decent",
    "efficient",
    "responsive",
    "pleased",
    # Hinglish
    "badhiya",
    "mast",
    "zabardast",
    "accha",
    "achha",
    "acha",
    "shandaar",
    "sahi",
    "paisa-vasool",
    "vasool",
    "khush",
    "bindaas",
    "jhakaas",
}
NEGATIVE = {
    "bad",
    "worst",
    "terrible",
    "awful",
    "poor",
    "hate",
    "hated",
    "disappointed",
    "disappointing",
    "useless",
    "delay",
    "delayed",
    "delays",
    "waiting",
    "wait",
    "broken",
    "issue",
    "issues",
    "problem",
    "problems",
    "bug",
    "buggy",
    "bugs",
    "crash",
    "crashes",
    "slow",
    "lag",
    "laggy",
    "expensive",
    "overpriced",
    "rude",
    "fraud",
    "scam",
    "complaint",
    "complaints",
    "fail",
    "failed",
    "failure",
    "faulty",
    "defect",
    "defective",
    "never",
    "refund",
    "stuck",
    "drains",
    "drain",
    "overheating",
    "heating",
    "noisy",
    "rattle",
    "cheap",
    "unreliable",
    "pathetic",
    "frustrating",
    "frustrated",
    "angry",
    "nightmare",
    "misleading",
    "lower",
    "less",
    "short",
    "dropped",
    "drops",
    "worse",
    "horrible",
    "regret",
    "annoying",
    "unhappy",
    # Hinglish
    "bekaar",
    "bakwas",
    "bekar",
    "kharab",
    "ghatiya",
    "pareshan",
    "pareshaan",
    "dhokha",
    "faltu",
    "bura",
    "nahi",
    "mat",
    "bechara",
}
INTENSIFIERS = {"very", "extremely", "really", "super", "totally", "bahut", "ekdum", "bilkul"}
NEGATORS = {"not", "no", "never", "dont", "don't", "didn't", "isn't", "wasn't", "nahin", "na"}
HINGLISH_MARKERS = {
    "hai",
    "hain",
    "nahi",
    "nahin",
    "bahut",
    "kya",
    "mera",
    "meri",
    "mere",
    "kharab",
    "bekaar",
    "bakwas",
    "accha",
    "achha",
    "acha",
    "yaar",
    "bhai",
    "kar",
    "raha",
    "rahi",
    "gaya",
    "gayi",
    "tha",
    "thi",
    "wala",
    "wali",
    "liya",
    "diya",
    "matlab",
    "abhi",
    "sab",
    "ekdum",
    "paisa",
    "kaafi",
    "mein",
    "toh",
    "bhi",
}


def sentiment_score(text: str) -> float:
    words = re.findall(r"[a-z'-]+", (text or "").lower())
    score = 0.0
    hits = 0
    for i, w in enumerate(words):
        polarity = 1.0 if w in POSITIVE else -1.0 if w in NEGATIVE else 0.0
        if polarity == 0.0:
            continue
        window = words[max(0, i - 3) : i]
        if any(n in NEGATORS for n in window):
            polarity = -polarity * 0.8
        if any(x in INTENSIFIERS for x in window):
            polarity *= 1.5
        score += polarity
        hits += 1
    if hits == 0:
        return 0.0
    value = score / (hits + 1.5)
    return round(max(-1.0, min(1.0, value)), 3)


def detect_language(text: str, hints: list[str] | None = None) -> str:
    words = set(re.findall(r"[a-z]+", (text or "").lower()))
    hinglish_hits = len(words & HINGLISH_MARKERS)
    if hinglish_hits >= 2 and (hints is None or "hi-en" in hints):
        return "hi-en"
    if re.search(r"[ऀ-ॿ]", text or ""):
        return "hi"
    return "en"


def detect_aspect(text: str, aspect_terms: dict[str, list[str]]) -> str:
    """Pick the aspect whose terms appear most in ``text`` (ties broken by config order)."""
    lower = (text or "").lower()
    best, best_hits = "other", 0
    for aspect, terms in aspect_terms.items():
        hits = 0
        for t in terms:
            if not t:
                continue
            hits += len(re.findall(rf"(?<![a-z]){re.escape(t.lower())}(?![a-z])", lower))
        if hits > best_hits:
            best, best_hits = aspect, hits
    return best


def best_quote(text: str, max_words: int = 25) -> str:
    """The sentence carrying the strongest sentiment, trimmed to ``max_words``."""
    sents = [s for s in split_sentences(text) if not looks_like_injection(s)]
    if not sents:
        return truncate_words(text or "", max_words)
    scored = sorted(sents, key=lambda s: (-abs(sentiment_score(s)), sents.index(s)))
    return truncate_words(scored[0], max_words)


# ---------------------------------------------------------------------------------------------
# Change classification (generic commerce vocabulary, not sector-specific)
# ---------------------------------------------------------------------------------------------

_CURRENCY_RE = re.compile(r"(₹|Rs\.?|INR|\$|USD|€|£)\s?\d")
_LAUNCH_WORDS = re.compile(
    r"(?i)\b(launch(?:ed|es)?|introduc(?:ing|es|ed)|all[- ]new|now available|pre-?book(?:ings?)?|unveil(?:s|ed)?|coming soon|new model|debut)\b"
)
_POLICY_WORDS = re.compile(
    r"(?i)\b(warranty|guarantee|terms|refund|return policy|subsid(?:y|ies)|policy|emi|finance|financing|insurance|buyback|exchange offer|cancellation)\b"
)
_DEALER_WORDS = re.compile(
    r"(?i)\b(dealers?|dealerships?|showrooms?|stores?|outlets?|experience cent(?:re|er)s?|service cent(?:re|er)s?|touchpoints?|cities)\b"
)
_FEATURE_WORDS = re.compile(
    r"(?i)\b(features?|mode|modes|update|updates|ota|software|app|connectivity|variant|colour|color|capacity|upgrade|upgraded|specs?|specification)\b"
)


def classify_change_rules(before: str, after: str) -> tuple[str, float]:
    """Return (kind, confidence) from deterministic rules. Price first, then launch/policy/dealer/feature."""
    b_prices = [n.value for n in extract_numbers(before) if n.has_currency]
    a_prices = [n.value for n in extract_numbers(after) if n.has_currency]
    inserted = after if not before else " ".join(sorted(set(split_sentences(after)) - set(split_sentences(before))))
    probe = inserted or after
    if b_prices and a_prices and sorted(b_prices) != sorted(a_prices):
        return "price", 0.9
    if _LAUNCH_WORDS.search(probe):
        return "launch", 0.75
    if (b_prices or a_prices) and sorted(b_prices) != sorted(a_prices):
        return "price", 0.7
    if _POLICY_WORDS.search(probe) or _POLICY_WORDS.search(before):
        return "policy", 0.7
    if _DEALER_WORDS.search(probe) or _DEALER_WORDS.search(before):
        return "dealer", 0.7
    if _FEATURE_WORDS.search(probe):
        return "feature", 0.6
    return "other", 0.3


# ---------------------------------------------------------------------------------------------
# Claimed / reported metric extraction
# ---------------------------------------------------------------------------------------------

_CLAIM_CONTEXT = re.compile(
    r"(?i)\b(claim(?:s|ed)?|company says|as per (?:the )?(?:brand|company)|certified|idc|arai|rated|official(?:ly)?|advertis(?:ed|es)|brochure|promised|quoted)\b"
)
_FIRST_HAND = re.compile(
    r"(?i)\b(i|i'm|im|i've|ive|my|mine|me|we|our|mera|meri|mere|mujhe|hum|humara|getting|got|get|gets|achieved|managed|real[- ]world|actual|daily)\b"
)


@dataclass
class ValueMention:
    value: float
    unit: str
    sentence: str


def _unit_pattern(units: list[str]) -> str:
    return "|".join(re.escape(u) for u in sorted(units, key=len, reverse=True))


def find_metric_values(text: str, units: list[str], keywords: list[str] | None = None) -> list[ValueMention]:
    """All ``<number> <unit>`` mentions, optionally requiring a metric keyword in the same sentence."""
    if not units:
        return []
    pat = re.compile(
        rf"(?i)(?<![\d.,])(\d+(?:\.\d+)?)\s?(?:-|to|–)?\s?(\d+(?:\.\d+)?)?\s?({_unit_pattern(units)})(?![a-z])"
    )
    out = []
    for sent in split_sentences(text):
        if keywords and not any(k in sent.lower() for k in keywords):
            continue
        for m in pat.finditer(sent):
            lo = float(m.group(1))
            hi = float(m.group(2)) if m.group(2) else None
            value = (lo + hi) / 2 if hi and hi > lo else lo
            out.append(ValueMention(value=value, unit=m.group(3).lower(), sentence=sent))
    return out


def extract_claimed_value(text: str, units: list[str], keywords: list[str]) -> ValueMention | None:
    """The claimed figure on an official page: the largest value in a sentence mentioning the metric."""
    candidates = [v for v in find_metric_values(text, units) if any(k in v.sentence.lower() for k in keywords)]
    if not candidates:
        return None
    return max(candidates, key=lambda v: v.value)


def extract_reported_value(text: str, units: list[str], keywords: list[str]) -> ValueMention | None:
    """A first-hand reported figure in a customer post (skipping quotes of the official claim)."""
    for v in find_metric_values(text, units, keywords):
        if _CLAIM_CONTEXT.search(v.sentence) and not re.search(r"(?i)\b(but|only|actual|real|sirf|bas)\b", v.sentence):
            continue
        if not _FIRST_HAND.search(v.sentence):
            continue
        return v
    return None


_CONDITION_PATTERNS = {
    "riding_style": re.compile(
        r"(?i)\b(eco|sport|city|highway|traffic|mixed|normal|ride|smart eco|heavy use|gaming|light use|moderate use)\b"
    ),
    "season": re.compile(r"(?i)\b(summer|winter|monsoon|rain|rainy|cold|hot)\b"),
}


def extract_conditions(sentence: str, known_places: list[str] | None = None) -> dict[str, str]:
    cond: dict[str, str] = {}
    for key, pat in _CONDITION_PATTERNS.items():
        m = pat.search(sentence)
        if m:
            cond[key] = m.group(1).lower()
    m = re.search(r"\bin ([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\b", sentence)
    if m and (not known_places or m.group(1) in known_places):
        cond["city"] = m.group(1)
    return cond


# ---------------------------------------------------------------------------------------------
# Entity resolution helper
# ---------------------------------------------------------------------------------------------


def resolve_entities(text: str, aliases: dict[str, str]) -> list[str]:
    return find_entities(text, aliases)


# ---------------------------------------------------------------------------------------------
# Ask router
# ---------------------------------------------------------------------------------------------

_INTENT_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "coordination",
        re.compile(
            r"(?i)\b(action items?|todo|to-do|owner|owners|due|overdue|follow[- ]?ups?|meeting|tasks?|assigned)\b"
        ),
    ),
    (
        "metrics",
        re.compile(
            r"(?i)\b(registrations?|market share|share|units|volumes?|sales|numbers|trend|mom|month over month|growth rate|metrics?)\b"
        ),
    ),
    (
        "opportunities",
        re.compile(
            r"(?i)\b(opportunit(?:y|ies)|risks?|recommend(?:ation)?s?|what should we|priorit(?:y|ies|ise|ize)|board)\b"
        ),
    ),
    (
        "customer_voice",
        re.compile(
            r"(?i)\b(customers?|owners?|users?|complain(?:t|ts|ing)?|sentiment|reviews?|reddit|youtube|people say|feedback|themes?|voice)\b"
        ),
    ),
    (
        "market_change",
        re.compile(
            r"(?i)\b(what changed|changes?|price cuts?|pricing|launch(?:es|ed)?|new models?|policy|subsid(?:y|ies)|this week|recently|latest)\b"
        ),
    ),
]


def route_intent(question: str, aliases: dict[str, str]) -> tuple[str, list[str], bool]:
    """(intent, entities, needs_research). Complex/multi-part questions need the research path."""
    entities = find_entities(question, aliases)
    intent = "general"
    for name, pat in _INTENT_RULES:
        if pat.search(question):
            intent = name
            break
    if intent == "general" and entities:
        intent = "competitor"
    complex_q = bool(
        re.search(r"(?i)\b(why|compare|versus|vs\.?|how should|implications?|explain|strategy)\b", question)
    )
    needs_research = intent in ("general", "competitor") or complex_q or len(question.split()) > 24
    return intent, entities, needs_research


# ---------------------------------------------------------------------------------------------
# Action-item extraction (heuristic fallback / mock)
# ---------------------------------------------------------------------------------------------

_ACTION_PREFIX = re.compile(
    r"(?i)^\s*(?:[-*•]\s*)?(?:\[\s?\]\s*)?(?:action(?: item)?s?|ai|todo|to-do|next steps?|follow[- ]up)\s*[:\-–]\s*"
)
_OWNER_VERB = re.compile(
    r"^(?:[-*•]\s*)?(?:@)?(?P<owner>[A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\s*(?:\((?P<paren>[^)]*)\))?\s*(?::|will|to|should|is going to|has agreed to|agreed to|owns|needs to|must|can|would)\s+(?P<rest>.+)$"
)
_CAN_OWNER = re.compile(
    r"^(?:[-*•]\s*)?(?:[Cc]an|[Cc]ould)\s+(?P<owner>[A-Z][a-z]+)\s+(?:also\s+|please\s+)?(?P<rest>.+?)\??$"
)
_AT_START = re.compile(r"^@(?P<owner>[A-Z][a-z]+)[,:]?\s+(?:please\s+|pls\s+|kindly\s+)?(?P<rest>.+)$")
_MULTI_OWNER = re.compile(
    r"^(?P<owner>[A-Z][a-z]+)(?:\s*(?:,|and|&)\s*[A-Z][a-z]+)+\s+(?:will|to|should|are going to|need to|must)\s+(?P<rest>.+)$"
)
_SOFT_PREFIX = re.compile(r"(?i)^(?:nice to have|optional|low priority|if time permits|stretch)\s*[:\-–]\s*")
_LIST_PREFIX = re.compile(r"^\s*(?:[-*•]\s*|\d{1,2}[.)]\s+)")
_AT_OWNER = re.compile(r"@(?P<owner>[A-Z][a-z]+)")
_UNASSIGNED_START = re.compile(
    r"(?i)^(?:[-*•]\s*)?(?:we|someone|somebody|the team|team|everyone|anyone|all|tbd|they|he|she|you)\b\s*(?:need to|needs to|should|must|will|have to|has to|could|can|to)\s+(?P<rest>.+)$"
)
_DUE_RE = re.compile(
    r"(?i)\b(?:by|before|due|on|until|no later than|latest by)?\s*"
    r"(?P<due>(?:end of (?:the )?(?:day|week|month|quarter))|eod|eow|eom|eoq|today|tonight|tomorrow|day after tomorrow|"
    r"(?:this|next) (?:week|month|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"in (?:\d+|one|two|three|four|five|six|seven|eight|nine|ten) (?:days?|weeks?)|"
    r"(?:\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*)|"
    r"(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?)|"
    r"\d{4}-\d{2}-\d{2})\b"
)
_NON_ACTION = re.compile(
    r"(?i)^(?:[-*•]\s*)?(?:we discussed|discussed|noted|fyi|update:|decision:|decided|agreed that|context:|background:|"
    r"attendees?:|date:|idea:|ideas:|note:|question:|risk:)"
)
_HIGH = re.compile(r"(?i)\b(urgent|asap|critical|blocker|top priority|immediately|high priority|p0|p1)\b")
_LOW = re.compile(r"(?i)\b(when possible|nice to have|low priority|if time permits|eventually|someday|p3)\b")
_PRONOUNS = {"he", "she", "they", "we", "someone", "somebody", "team", "everyone", "anyone", "you", "i"}


def parse_attendees(note: str) -> list[str]:
    m = re.search(r"(?im)^\s*(?:attendees|participants|present)\s*:\s*(.+)$", note)
    if not m:
        return []
    names = re.split(r"[,;/]| and ", m.group(1))
    out = []
    for n in names:
        n = re.sub(r"\(.*?\)", "", n).strip()
        if n and n[0].isupper():
            out.append(n.split()[0])
    return out


def _clean_desc(text: str) -> str:
    text = _DUE_RE.sub(lambda m: "" if m.group("due") else m.group(0), text)
    text = re.sub(r"(?i)\b(by|before|due|until|no later than|latest by)\s*$", "", text.strip())
    text = re.sub(r"(?i)\s*\((?:urgent|asap|high priority|low priority)\)\s*", " ", text)
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" .,;:-")
    return text[:1].upper() + text[1:] if text else text


def extract_action_items_heuristic(note: str) -> list[dict[str, object]]:
    attendees = parse_attendees(note)
    items: list[dict[str, object]] = []
    lines = []
    for raw in note.splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        # Split bullet lines that contain several sentences.
        lines.extend(split_sentences(raw) if not raw.startswith(("-", "*", "•")) else [raw])
    for line in lines:
        core = _LIST_PREFIX.sub("", line)
        if _NON_ACTION.match(core) or looks_like_injection(line):
            continue
        body = _ACTION_PREFIX.sub("", core)
        is_marked = body != core
        soft = _SOFT_PREFIX.sub("", body)
        is_soft = soft != body
        body = soft
        owner: str | None = None
        rest: str | None = None
        flags: list[str] = []
        m = _OWNER_VERB.match(body)
        mm = _MULTI_OWNER.match(body)
        at0 = _AT_START.match(body)
        if mm and mm.group("owner").lower() not in _PRONOUNS:
            owner, rest = mm.group("owner"), mm.group("rest")
            flags.append("multiple_owners")
        elif at0:
            owner, rest = at0.group("owner"), at0.group("rest")
        elif m and m.group("owner").split()[0].lower() not in _PRONOUNS:
            owner = m.group("owner").split()[0]
            rest = m.group("rest")
        else:
            m2 = _CAN_OWNER.match(body)
            if m2 and m2.group("owner").lower() not in _PRONOUNS:
                owner, rest = m2.group("owner"), m2.group("rest")
            else:
                m3 = _UNASSIGNED_START.match(body)
                if m3:
                    rest = m3.group("rest")
                    flags.append("ambiguous_owner")
                elif is_marked:
                    rest = body
                    at = _AT_OWNER.search(body)
                    if at:
                        owner = at.group("owner")
                        rest = _AT_OWNER.sub("", body).strip()
                    else:
                        flags.append("missing_owner")
        if rest is None:
            continue
        # Only keep lines that read like tasks (a verb-led clause or an explicit action marker).
        if not is_marked and owner is None and "ambiguous_owner" not in flags:
            continue
        if owner and attendees and owner not in attendees:
            flags.append("owner_not_in_attendees")
        due = _DUE_RE.search(rest)
        due_text = due.group("due") if due and due.group("due") else None
        priority = "high" if _HIGH.search(line) else "low" if (is_soft or _LOW.search(line)) else "medium"
        description = _clean_desc(rest)
        if len(description.split()) < 2:
            continue
        confidence = 0.85 if owner and due_text else 0.7 if owner or due_text else 0.55
        items.append(
            {
                "description": description,
                "owner": owner,
                "due_text": due_text,
                "priority": priority,
                "confidence": confidence,
                "source_sentence": normalize_ws(line),
                "flags": flags,
            }
        )
    return items


# ---------------------------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------------------------


def top_terms(texts: list[str], k: int = 5) -> list[str]:
    counts: dict[str, int] = {}
    for t in texts:
        for tok in set(tokenize(t, keep_numbers=False)):
            counts[tok] = counts.get(tok, 0) + 1
    return [w for w, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:k]]
