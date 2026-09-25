"""Rule-based policies that stand in for the LLM agents when running with the MockClient.

They are intentionally simple and transparent, but they exercise exactly the same code paths
(tool loop, claim schema, critic, scoring, writer post-checks) as the real model does.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from fieldnote.heuristics import sentiment_score
from fieldnote.llm.base import ChatResult, ToolCall
from fieldnote.textutil import (
    direction_of,
    extract_numbers,
    find_entities,
    fmt_number,
    looks_like_injection,
    number_supported,
    split_sentences,
    tokenize,
    truncate_words,
    unwrap_untrusted,
)

GENERIC_CLAIM_WORDS = {
    "estimate",
    "customer",
    "customers",
    "post",
    "posts",
    "says",
    "said",
    "reported",
    "reports",
    "owner",
    "owners",
    "claimed",
    "claim",
    "median",
    "iqr",
    "tracked",
    "regions",
    "confidence",
    "low",
    "month",
    "past",
    "weeks",
    "week",
    "mean",
    "sentiment",
    "theme",
    "themes",
    "clustered",
    "under",
    "accounts",
    "accounted",
    "listed",
    "page",
    "lists",
    "fieldnote",
    "captured",
    "summary",
    "against",
    "gap",
    "trailing",
    "vs",
    "across",
    "share",
    "percent",
    "units",
}

# ---------------------------------------------------------------------------------------------
# Tool-result parsing
# ---------------------------------------------------------------------------------------------


def _tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.get("role") != "user" or not isinstance(m.get("content"), list):
            continue
        for block in m["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                content = block.get("content")
                if isinstance(content, list):
                    content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
                try:
                    data = json.loads(content or "{}")
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    out.append(data)
    return out


def _assistant_turns(messages: list[dict[str, Any]]) -> int:
    return sum(1 for m in messages if m.get("role") == "assistant")


def _tool_use(step: int, i: int, name: str, args: dict[str, Any]) -> tuple[dict[str, Any], ToolCall]:
    tid = f"toolu_mock_{step}_{i}"
    return {"type": "tool_use", "id": tid, "name": name, "input": args}, ToolCall(id=tid, name=name, input=args)


# ---------------------------------------------------------------------------------------------
# Researcher
# ---------------------------------------------------------------------------------------------


def researcher_step(
    messages: list[dict[str, Any]], payload: dict[str, Any], force_tool: str | None = None
) -> ChatResult:
    step = _assistant_turns(messages)
    results = _tool_results(messages)
    calls: list[tuple[str, dict[str, Any]]] = []
    if force_tool == "submit_claims" or step >= 2:
        output = build_research_output(results, payload)
        block, call = _tool_use(step, 0, "submit_claims", output)
        return ChatResult(
            text="",
            tool_calls=[call],
            stop_reason="tool_use",
            assistant_content=[{"type": "text", "text": "Submitting sourced claims."}, block],
        )
    if step == 0:
        if not payload.get("focus"):
            calls += [
                ("get_changes", {"since": payload.get("since"), "min_significance": 0.3}),
                ("get_metric_summary", {}),
                ("get_claim_vs_reported", {}),
                ("get_themes", {}),
            ]
        for q in payload.get("queries", [])[:10]:
            calls.append(("search_documents", {"query": q, "k": 6}))
        # News-only searches per tracked entity so commentary and official releases are not crowded out.
        for ent in payload.get("entities", [])[:8]:
            calls.append(("search_documents", {"query": ent, "k": 5, "filters": {"source_type": "news"}}))
    else:
        calls = [("get_document", {"id": i}) for i in _pick_documents(results, payload)]
    if not calls:
        output = build_research_output(results, payload)
        block, call = _tool_use(step, 0, "submit_claims", output)
        return ChatResult(text="", tool_calls=[call], stop_reason="tool_use", assistant_content=[block])
    content: list[dict[str, Any]] = [{"type": "text", "text": f"Gathering evidence (step {step + 1})."}]
    tool_calls = []
    for i, (name, args) in enumerate(calls):
        block, call = _tool_use(step, i, name, args)
        content.append(block)
        tool_calls.append(call)
    return ChatResult(text=content[0]["text"], tool_calls=tool_calls, stop_reason="tool_use", assistant_content=content)


def _pick_documents(results: list[dict[str, Any]], payload: dict[str, Any]) -> list[int]:
    """Reading list: change records and datasets first, then primary releases and news, then samples."""
    ordered: list[int] = []

    def add(i: Any) -> None:
        if isinstance(i, int) and i not in ordered:
            ordered.append(i)

    for r in results:
        if r.get("tool") == "get_changes":
            for ch in r.get("changes", []):
                add(ch.get("document_id"))
    for r in results:
        if r.get("tool") == "get_metric_summary":
            for c in r.get("connectors", []):
                add(c.get("dataset_document_id"))
    hits: list[dict[str, Any]] = []
    for r in results:
        if r.get("tool") == "search_documents":
            hits.extend(r.get("results", []))
    best: dict[int, dict[str, Any]] = {}
    for h in hits:
        if h.get("source_type") == "news" and (
            h["id"] not in best or h.get("score", 0) > best[h["id"]].get("score", 0)
        ):
            best[h["id"]] = h
    news = sorted(best.values(), key=lambda h: (not h.get("is_primary"), -float(h.get("score", 0)), h["id"]))
    for h in news[:12]:
        add(h.get("id"))
    for r in results:
        if r.get("tool") == "get_themes":
            for t in r.get("themes", []):
                if (t.get("sentiment_mean", 0) <= -0.15 or t.get("is_emerging")) and t.get("sample_document_ids"):
                    add(t["sample_document_ids"][0])
    pages = [h for h in hits if h.get("source_type") == "page" and h.get("kind") not in ("page_change", "dataset")]
    for h in pages[:3]:
        add(h.get("id"))
    for h in [h for h in hits if h.get("source_type") in ("reddit", "youtube")][:4]:
        add(h.get("id"))
    limit = int(payload.get("max_documents", 24))
    return ordered[:limit]


def _docs_from_results(results: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    docs: dict[int, dict[str, Any]] = {}
    for r in results:
        if r.get("tool") == "get_document" and isinstance(r.get("document"), dict):
            d = dict(r["document"])
            d["content"] = unwrap_untrusted(d.get("content", ""))
            d["title"] = unwrap_untrusted(d.get("title", ""))
            docs[int(d["id"])] = d
    return docs


def _topic_tags(sentence: str) -> list[str]:
    s = sentence.lower()
    tags = []
    if re.search(r"(₹|rs\.?|inr|\$)\s?\d|price|priced|pricing|discount|cost", s):
        tags.append("price")
    if re.search(r"launch|introduc|unveil|new model|debut", s):
        tags.append("launch")
    if re.search(r"subsid|policy|regulat|warranty|scheme|incentive|tax|gst|rule", s):
        tags.append("policy")
    if re.search(r"dealer|(?<!ex-)showroom|store|outlet|network|experience cent", s):
        tags.append("dealer")
    if re.search(r"sales|sold|registrations?|shipments?|volumes?|units|market share", s):
        tags.append("sales")
    if re.search(r"\bfunding\b|\braised\b|\binvest(?:ment|ed|ors?)?\b|\bipo\b|\bvaluation\b", s):
        tags.append("funding")
    if re.search(r"recall|fire|safety|defect", s):
        tags.append("safety")
    d = direction_of(sentence)
    if d > 0:
        tags.append("up")
    elif d < 0:
        tags.append("down")
    return tags


def _sentence_claims(
    doc: dict[str, Any], aliases: dict[str, str], focus_tokens: set[str], per_doc: int, ref_start: int
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sent in split_sentences(doc.get("content", "")):
        if len(out) >= per_doc:
            break
        if looks_like_injection(sent) or len(sent.split()) < 6 or sent.count("|") >= 2:
            continue
        if sent.lower().startswith(("before (", "after (", "summary:", "page:")):
            continue
        ents = find_entities(sent, aliases)
        nums = extract_numbers(sent)
        tags = _topic_tags(sent)
        topical = [t for t in tags if t not in ("up", "down")]
        if focus_tokens:
            if not (set(tokenize(sent)) & focus_tokens) and not (set(e.lower() for e in ents) & focus_tokens):
                continue
        elif ents:
            if not (nums or topical):
                continue
        elif not (set(topical) & {"policy", "sales"} and nums):
            continue  # market-level statements only when they carry a figure about policy or volumes
        text = truncate_words(sent, 45, ellipsis="")
        out.append(
            {
                "ref": f"c{ref_start + len(out)}",
                "type": "fact",
                "text": text,
                "source_document_ids": [int(doc["id"])],
                "excerpt": text,
                "entities": ents,
                "tags": ["news" if doc.get("source_type") == "news" else doc.get("source_type", "other"), *tags],
                "aspect": None,
                "derivation": None,
            }
        )
    return out


def build_research_output(results: list[dict[str, Any]], payload: dict[str, Any]) -> dict[str, Any]:
    aliases: dict[str, str] = payload.get("aliases", {})
    aspect_terms: dict[str, list[str]] = payload.get("aspect_terms", {})
    max_claims = int(payload.get("max_claims", 40))
    focus = payload.get("focus") or ""
    focus_tokens = set(tokenize(focus, keep_numbers=False)) if focus else set()
    focus_tokens |= {e.lower() for e in find_entities(focus, aliases)} if focus else set()
    docs = _docs_from_results(results)
    claims: list[dict[str, Any]] = []

    def ref() -> str:
        return f"c{len(claims) + 1}"

    # A. Page changes (primary: FieldNote's own capture of official pages).
    for r in results:
        if r.get("tool") != "get_changes":
            continue
        for ch in r.get("changes", []):
            if ch.get("significance", 0) < 0.3 or not ch.get("document_id"):
                continue
            tags = ["change", ch.get("kind", "other")]
            pct = (ch.get("details") or {}).get("price_change_pct")
            if pct is not None:
                tags.append("price_down" if pct < 0 else "price_up")
            claims.append(
                {
                    "ref": ref(),
                    "type": "fact",
                    "text": ch["summary"],
                    "source_document_ids": [ch["document_id"]],
                    "excerpt": ch["summary"],
                    "entities": [ch["competitor"]],
                    "tags": tags,
                    "aspect": None,
                    "derivation": None,
                }
            )

    # B. Metric connectors (estimates computed in code; the dataset summary document is the source).
    for r in results:
        if r.get("tool") != "get_metric_summary":
            continue
        for conn in r.get("connectors", []):
            doc_id = conn.get("dataset_document_id")
            if not doc_id:
                continue
            label = conn.get("label") or "volume"
            unit = conn.get("unit_short") or "units"
            by_entity = {e["entity"]: e for e in conn.get("entities", [])}
            movers = [m for m in conn.get("movers", []) if m in by_entity][:2]
            for name in movers:
                e = by_entity[name]
                if e.get("pct_change") is None or not e.get("previous"):
                    continue
                pct = round(e["pct_change"], 1)
                verb = "rose" if pct > 0 else "fell"
                claims.append(
                    {
                        "ref": ref(),
                        "type": "estimate",
                        "text": (
                            f"Estimate: {name} {label} across tracked regions {verb} {abs(pct):.1f}% month over month "
                            f"in {conn['latest_period']} ({fmt_number(e['latest'])} vs {fmt_number(e['previous'])} {unit})."
                        ),
                        "source_document_ids": [doc_id],
                        "excerpt": e["line"],
                        "entities": [name],
                        "tags": ["metric", "metric_up" if pct > 0 else "metric_down"],
                        "aspect": None,
                        "derivation": {
                            "method": "percent change of summed monthly values across tracked regions",
                            "op": "pct_change",
                            "operands": [e["previous"], e["latest"]],
                            "result": pct,
                        },
                    }
                )
            leader = max(conn.get("entities", []), key=lambda x: x.get("latest") or 0, default=None)
            if leader and conn.get("total_latest"):
                share = round(leader["latest"] / conn["total_latest"] * 100, 1)
                claims.append(
                    {
                        "ref": ref(),
                        "type": "estimate",
                        "text": (
                            f"Estimate: {leader['entity']} accounted for {share:.1f}% of tracked {label} in "
                            f"{conn['latest_period']} ({fmt_number(leader['latest'])} of {fmt_number(conn['total_latest'])} {unit})."
                        ),
                        "source_document_ids": [doc_id],
                        "excerpt": leader["line"],
                        "entities": [leader["entity"]],
                        "tags": ["metric", "share"],
                        "aspect": None,
                        "derivation": {
                            "method": "entity value divided by the total across tracked entities",
                            "op": "share",
                            "operands": [leader["latest"], conn["total_latest"]],
                            "result": share,
                        },
                    }
                )

    # C. Claim vs reported (reference estimates, verified against FieldNote's own table by the critic).
    for r in results:
        if r.get("tool") != "get_claim_vs_reported":
            continue
        for row in r.get("rows", []):
            if row.get("claimed_value") is None or row.get("reported_median") is None or row.get("n", 0) < 3:
                continue
            samples = row.get("samples") or []
            if not samples:
                continue
            metric = row["metric"].replace("_", " ")
            unit = row.get("unit", "")
            med, q1, q3, n = row["reported_median"], row["reported_q1"], row["reported_q3"], row["n"]
            claimed = row["claimed_value"]
            gap = round((claimed - med) / claimed * 100) if claimed else 0
            low = "low_confidence" in (row.get("flags") or [])
            if low:
                text = (
                    f"Estimate (low confidence, n={n}): owner-reported {metric} for {row['entity']} has a median of "
                    f"{_num(med)} {unit} (IQR {_num(q1)}-{_num(q3)}) against a claimed {_num(claimed)} {unit}."
                )
            else:
                text = (
                    f"Estimate: owner-reported {metric} for {row['entity']} has a median of {_num(med)} {unit} "
                    f"(IQR {_num(q1)}-{_num(q3)}, n={n}) against a claimed {_num(claimed)} {unit}, a gap of {gap}%."
                )
            srcs = [s["document_id"] for s in samples[:3]]
            if row.get("claimed_source_id"):
                srcs.append(row["claimed_source_id"])
            claims.append(
                {
                    "ref": ref(),
                    "type": "estimate",
                    "text": text,
                    "source_document_ids": srcs,
                    "excerpt": samples[0]["excerpt"],
                    "entities": [row["entity"]],
                    "tags": ["claim_vs_reported", "low_confidence" if low else "gap" if gap >= 15 else "aligned"],
                    "aspect": row["metric"],
                    "derivation": {
                        "method": "median and interquartile range of first-hand owner reports vs the claimed figure",
                        "op": "reference",
                        "reference": {"table": "claim_vs_reported", "metric": row["metric"], "entity": row["entity"]},
                    },
                }
            )

    # D. Customer-voice themes (reference estimates) and representative posts (facts).
    for r in results:
        if r.get("tool") != "get_themes":
            continue
        for t in r.get("themes", []):
            neg = t.get("sentiment_mean", 0) <= -0.15
            if not (neg or t.get("is_emerging")) or not t.get("sample_document_ids"):
                continue
            top_entity = t.get("top_entity")
            aspect = t.get("top_aspect") or "other"
            text = (
                f"Estimate: {t['size']} customer posts in the current window clustered under the theme "
                f"'{t['label']}', with mean sentiment {t['sentiment_mean']:+.2f}"
            )
            ents: list[str] = []
            if top_entity and t.get("top_entity_count", 0) >= max(2, t["size"] * 0.4):
                text += f"; {t['top_entity_count']} of them mention {top_entity}"
                ents = [top_entity]
            if t.get("is_emerging") and t["size"] != t.get("size_prev", 0):
                trend = "up" if t["size"] > t.get("size_prev", 0) else "down"
                text += f" ({trend} from {t.get('size_prev', 0)} in the previous window)"
            first = next((docs[i] for i in t["sample_document_ids"] if i in docs), None)
            excerpt = _body_sentences(first)[0] if first and _body_sentences(first) else ""
            cited = list(t["sample_document_ids"][:3])
            if first is not None and first["id"] not in cited:
                cited = [first["id"], *cited[:2]]
            claims.append(
                {
                    "ref": ref(),
                    "type": "estimate",
                    "text": text + ".",
                    "source_document_ids": cited,
                    "excerpt": truncate_words(excerpt, 40, ""),
                    "entities": ents,
                    "tags": [
                        "voice",
                        "voice_negative" if neg else "voice_mixed",
                        *(["emerging"] if t.get("is_emerging") else []),
                    ],
                    "aspect": aspect,
                    "derivation": {
                        "method": "TF-IDF + KMeans clustering of tagged customer posts; counts and mean sentiment computed in code",
                        "op": "reference",
                        "reference": {"table": "themes", "theme_id": str(t["id"])},
                    },
                }
            )

    # E. News / official pages: sentence-level facts (primary sources first).
    ordered_docs = sorted(
        docs.values(),
        key=lambda d: (not d.get("is_primary"), {"page": 0, "news": 1}.get(d.get("source_type", ""), 2), d["id"]),
    )
    for d in ordered_docs:
        if d.get("source_type") not in ("news", "page"):
            continue
        if d.get("meta_kind") in ("page_change", "dataset"):
            continue
        for c in _sentence_claims(d, aliases, focus_tokens, per_doc=2, ref_start=len(claims) + 1):
            c["ref"] = ref()
            claims.append(c)

    # F. Customer posts: one representative quote each (facts about what a post says).
    for d in ordered_docs:
        if d.get("source_type") not in ("reddit", "youtube") or looks_like_injection(d.get("content", "")):
            continue
        sents = [s for s in _body_sentences(d) if len(s.split()) >= 5 and not looks_like_injection(s)]
        if focus_tokens:
            sents = [s for s in sents if set(tokenize(s)) & focus_tokens] or []
        if not sents:
            continue
        sent = min(
            sents,
            key=lambda s: (
                sentiment_score(s),
                not (extract_numbers(s) or _aspect_for(s, aspect_terms)),
                sents.index(s),
            ),
        )
        sent = truncate_words(sent, 35, "")
        post_ents = find_entities(sent, aliases)
        aspect = _aspect_for(sent, aspect_terms)
        sentiment = sentiment_score(sent)
        claims.append(
            {
                "ref": ref(),
                "type": "fact",
                "text": f'A customer post says: "{sent}"',
                "source_document_ids": [d["id"]],
                "excerpt": sent,
                "entities": post_ents,
                "tags": [
                    "voice",
                    "voice_negative" if sentiment < -0.1 else "voice_positive" if sentiment > 0.1 else "voice_mixed",
                ],
                "aspect": aspect,
                "derivation": None,
            }
        )

    if focus_tokens:
        claims = [c for c in claims if (set(tokenize(c["text"])) & focus_tokens) or c["type"] == "estimate"]
    claims = claims[:max_claims]
    for i, c in enumerate(claims, start=1):
        c["ref"] = f"c{i}"
    return {"claims": claims, "disagreements": _disagreements(claims), "notes": "mock researcher"}


def _body_sentences(d: dict[str, Any] | None) -> list[str]:
    """Sentences of a document's body (a post's title line is skipped when there is a body)."""
    if not d:
        return []
    sents = split_sentences(d.get("content", ""))
    title = (d.get("title") or "").strip()
    if len(sents) > 1 and title and sents[0].strip().rstrip(".") == title.rstrip("."):
        sents = sents[1:]
    return sents


def _num(v: float) -> str:
    return fmt_number(v, 0) if abs(v - round(v)) < 1e-9 else fmt_number(v, 1)


def _aspect_for(text: str, aspect_terms: dict[str, list[str]]) -> str | None:
    from fieldnote.heuristics import detect_aspect

    a = detect_aspect(text, aspect_terms)
    return None if a == "other" else a


_DELTA_PATTERNS = [
    re.compile(r"(?i)\b(?:by|of)\s+((?:₹|rs\.?\s?|inr\s?|\$)\s?[\d,]+(?:\.\d+)?(?:\s?(?:lakh|crore|k))?)"),
    re.compile(
        r"(?i)((?:₹|rs\.?\s?|inr\s?|\$)\s?[\d,]+(?:\.\d+)?)\s+(?:cheaper|less|lower|off|costlier|more expensive|higher)\b"
    ),
    re.compile(r"(?i)\b(?:by|of)\s+(\d+(?:\.\d+)?\s?%)"),
]


def _change_amounts(text: str) -> list[float]:
    """Reported size of a change ("cut by ₹10,000", "₹12,000 cheaper", "rose by 12%")."""
    out = []
    for pat in _DELTA_PATTERNS:
        for m in pat.finditer(text):
            nums = extract_numbers(m.group(1))
            if nums:
                out.append(abs(nums[0].value))
    return out


def _disagreements(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Two sources reporting different magnitudes for the same entity/topic change are recorded, not resolved."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for c in claims:
        if c["type"] != "fact" or "news" not in c["tags"]:
            continue
        for t in ("price", "sales", "funding"):
            if t in c["tags"]:
                for e in c["entities"]:
                    groups[(e, t)].append(c)
    out = []
    for (entity, topic), items in sorted(groups.items()):
        amounts = {c["ref"]: _change_amounts(c["text"]) for c in items}
        refs = [r for r in sorted(amounts) if amounts[r]]
        for i in range(len(refs)):
            for j in range(i + 1, len(refs)):
                a = items_by_ref(items, refs[i])
                b = items_by_ref(items, refs[j])
                if a["source_document_ids"][:1] == b["source_document_ids"][:1]:
                    continue
                va, vb = amounts[refs[i]][0], amounts[refs[j]][0]
                if abs(va - vb) > 1e-6 * max(va, vb, 1):
                    out.append(
                        {
                            "description": (
                                f"Sources report different {topic} change figures for {entity}: "
                                f"{fmt_number(va)} vs {fmt_number(vb)}."
                            ),
                            "claim_refs": [refs[i], refs[j]],
                            "source_document_ids": [a["source_document_ids"][0], b["source_document_ids"][0]],
                        }
                    )
    return out[:10]


def items_by_ref(items: list[dict[str, Any]], r: str) -> dict[str, Any]:
    return next(c for c in items if c["ref"] == r)


# ---------------------------------------------------------------------------------------------
# Analyst
# ---------------------------------------------------------------------------------------------

_CATEGORY_TEMPLATES: dict[str, dict[str, Any]] = {
    "price_down": {
        "category": "risk",
        "title": "{E} price cut puts pressure on entry pricing",
        "why": "For a {P}, a lower {E} price narrows the room to compete on sticker price and can reset buyer expectations.",
        "analysis": "These signals suggest {E} is competing more aggressively on price.",
        "action": "Review entry-variant pricing and financing offers against {E}'s new price point before matching it.",
        "how": "Pricing lead compares effective on-road cost within 1 week; Finance models margin impact next; Sales lead briefs channel partners once a response is agreed (target 2 weeks).",
        "ratings": (4, 3, 2),
    },
    "price_up": {
        "category": "opportunity",
        "title": "{E} price increase opens a value gap",
        "why": "For a {P}, a pricier {E} line leaves space for a better-value alternative.",
        "analysis": "These signals suggest {E} is moving up-market, leaving value-seeking buyers exposed.",
        "action": "Position a value-for-money comparison against {E} in sales and digital channels.",
        "how": "Marketing lead drafts comparison messaging within 1 week; Sales lead validates with dealers; launch in 2 weeks.",
        "ratings": (3, 4, 2),
    },
    "launch": {
        "category": "risk",
        "title": "{E} expands its lineup",
        "why": "For a {P}, a new {E} product or variant raises the bar in the segments it targets.",
        "analysis": "These signals suggest {E} is broadening its lineup, which may crowd adjacent segments.",
        "action": "Benchmark the new {E} offering and decide whether roadmap or messaging needs to respond.",
        "how": "Product lead runs a spec and price benchmark within 2 weeks; Marketing adjusts positioning; leadership reviews at the next monthly planning cycle.",
        "ratings": (3, 2, 4),
    },
    "policy_up": {
        "category": "risk",
        "title": "{E} sweetens ownership terms",
        "why": "For a {P}, better {E} terms raise the baseline buyers will expect.",
        "analysis": "These signals suggest {E} is using ownership terms to reduce purchase risk for buyers.",
        "action": "Compare warranty, finance and after-sales terms with {E} and close any visible gaps.",
        "how": "After-sales lead audits current terms within 1 week; Finance prices any extension; Legal reviews before publishing (target 3 weeks).",
        "ratings": (3, 3, 2),
    },
    "policy_down": {
        "category": "opportunity",
        "title": "{E} policy change creates a switching window",
        "why": "For a {P}, less generous {E} terms can push hesitant buyers to compare alternatives.",
        "analysis": "These signals suggest buyers considering {E} now face weaker terms.",
        "action": "Highlight stronger ownership terms in messaging aimed at {E} considerers.",
        "how": "Marketing lead prepares a comparison within 1 week; Sales lead trains staff; run for 4 weeks and measure enquiries.",
        "ratings": (3, 4, 2),
    },
    "dealer": {
        "category": "risk",
        "title": "{E} widens its retail footprint",
        "why": "For a {P}, more {E} touchpoints make it easier for buyers to test-ride, buy and get service.",
        "analysis": "These signals suggest {E} is investing in distribution reach.",
        "action": "Map {E}'s new locations against planned coverage and prioritise overlapping markets.",
        "how": "Network lead maps locations within 2 weeks; Regional managers flag priority cities; leadership decides on pop-up or partner coverage within 6 weeks.",
        "ratings": (3, 2, 4),
    },
    "feature": {
        "category": "risk",
        "title": "{E} ships new product features",
        "why": "For a {P}, new {E} features can shift what buyers treat as standard.",
        "analysis": "These signals suggest {E} is differentiating on product features.",
        "action": "Assess whether the new {E} features matter to target buyers before reacting.",
        "how": "Product lead reviews the features within 1 week; Research tests relevance with recent buyers; decide on roadmap changes within 4 weeks.",
        "ratings": (2, 2, 3),
    },
    "metric_up": {
        "category": "risk",
        "title": "{E} is gaining momentum in tracked {L}",
        "why": "For a {P}, a competitor gaining momentum can crowd out newer brands in the same markets.",
        "analysis": "These signals suggest {E}'s recent momentum is outpacing the tracked market.",
        "action": "Identify which regions and segments drive {E}'s growth and protect those most exposed.",
        "how": "Analyst breaks down the growth by region within 1 week; Sales leads for affected regions propose responses; review in 2 weeks.",
        "ratings": (4, 3, 3),
    },
    "metric_down": {
        "category": "opportunity",
        "title": "{E} is losing momentum in tracked {L}",
        "why": "For a {P}, a slowing competitor frees up buyers and channel attention.",
        "analysis": "These signals suggest {E}'s momentum is weakening relative to the tracked market.",
        "action": "Target regions where {E} is slowing with focused offers and channel incentives.",
        "how": "Analyst confirms the regions within 1 week; Marketing launches targeted offers in 2 weeks; measure share after 1 month.",
        "ratings": (3, 3, 3),
    },
    "cvr_gap": {
        "category": "opportunity",
        "title": "Owner-reported {M} for {E} trails its claim",
        "why": "For a {P}, a visible gap between claimed and real-world {M} is a credibility opening.",
        "analysis": "These signals suggest buyers of {E} are experiencing less {M} than advertised.",
        "action": "Publish conservative, verified real-world {M} figures and make them a trust message.",
        "how": "Product lead documents test conditions within 2 weeks; Marketing publishes real-world figures; Customer support tracks sentiment for 1 month.",
        "ratings": (3, 4, 2),
    },
    "voice_negative": {
        "category": "opportunity",
        "title": "Customer frustration with {E} {A} is an opening",
        "why": "For a {P}, recurring {A} complaints about {E} show where buyers feel under-served.",
        "analysis": "These signals suggest {A} is a pain point for {E} customers.",
        "action": "Make {A} a visible strength in the offer and in conversations with {E} considerers.",
        "how": "Customer experience lead defines a {A} promise within 2 weeks; Operations confirms it is deliverable; Marketing runs it for 1 month and tracks sentiment.",
        "ratings": (3, 4, 3),
    },
    "market_policy": {
        "category": "risk",
        "title": "Market-wide policy change affects price-sensitive buyers",
        "why": "For a {P}, policy shifts change effective prices for every brand at once, which hits price-sensitive segments first.",
        "analysis": "These signals suggest a market-wide policy change is altering effective prices.",
        "action": "Model the effective-price impact of the policy change and prepare a pricing or financing response.",
        "how": "Finance models the impact within 1 week; Pricing lead proposes options; leadership decides within 2 weeks.",
        "ratings": (4, 2, 2),
    },
}


def _category_for(c: dict[str, Any]) -> tuple[str, str | None] | None:
    tags = set(c.get("tags", []))
    ents = c.get("entities") or []
    e = ents[0] if ents else None
    if "change" in tags:
        if "price" in tags:
            return ("price_down" if "price_down" in tags else "price_up", e)
        if "launch" in tags:
            return ("launch", e)
        if "policy" in tags:
            return ("policy_down" if direction_of(c["text"]) < 0 else "policy_up", e)
        if "dealer" in tags:
            return ("dealer", e)
        if "feature" in tags:
            return ("feature", e)
        return None
    if "metric_up" in tags:
        return ("metric_up", e)
    if "metric_down" in tags:
        return ("metric_down", e)
    if "claim_vs_reported" in tags and "gap" in tags:
        return ("cvr_gap", e)
    if "voice" in tags and "voice_negative" in tags and c["type"] == "estimate" and e:
        return ("voice_negative", e)
    if "news" in tags and "policy" in tags and not ents:
        return ("market_policy", None)
    if "news" in tags and e:
        if "launch" in tags:
            return ("launch", e)
        if "price" in tags and "down" in tags:
            return ("price_down", e)
        if "dealer" in tags and "up" in tags:
            return ("dealer", e)
    return None


def analyst(p: dict[str, Any]) -> dict[str, Any]:
    claims: list[dict[str, Any]] = p.get("claims", [])
    perspective = p.get("perspective", "market participant")
    shares: dict[str, float] = p.get("entity_shares", {})
    label = p.get("metric_label", "volumes")
    groups: dict[tuple[str, str | None], list[dict[str, Any]]] = defaultdict(list)
    anchors: dict[tuple[str, str | None], dict[str, Any]] = {}
    for c in claims:
        key = _category_for(c)
        if key is None:
            continue
        groups[key].append(c)
        anchors.setdefault(key, c)
    findings = []
    for key in sorted(groups, key=lambda k: (k[0], k[1] or "")):
        cat, entity = key
        tpl = _CATEGORY_TEMPLATES[cat]
        anchor = anchors[key]
        evidence = list(groups[key])
        # Attach corroborating claims: same entity and same topic/aspect.
        for c in claims:
            if c in evidence:
                continue
            same_entity = entity is not None and entity in (c.get("entities") or [])
            if not same_entity:
                continue
            if (
                (
                    cat == "voice_negative"
                    and c.get("aspect") == anchor.get("aspect")
                    and "voice_negative" in c.get("tags", [])
                )
                or (cat == "cvr_gap" and c.get("aspect") == anchor.get("aspect") and "voice" in c.get("tags", []))
                or (cat in ("price_down", "price_up") and "price" in c.get("tags", []) and "news" in c.get("tags", []))
                or (cat == "launch" and "launch" in c.get("tags", []))
                or (cat == "dealer" and "dealer" in c.get("tags", []))
                or (cat in ("policy_up", "policy_down") and "policy" in c.get("tags", []))
                or (cat in ("metric_up", "metric_down") and "metric" in c.get("tags", []))
            ):
                evidence.append(c)
        evidence = evidence[:6]
        ev_ids = [int(c["id"]) for c in evidence]
        subs = {
            "E": entity or "the market",
            "P": perspective,
            "L": label,
            "M": (anchor.get("aspect") or "performance").replace("_", " "),
            "A": (anchor.get("aspect") or "service").replace("_", " "),
        }
        ms, gap, effort = tpl["ratings"]
        share = shares.get(entity or "")
        if share is not None:
            ms += 1 if share >= 25 else -1 if share < 8 else 0
        if len({s for c in evidence for s in c.get("source_document_ids", [])}) >= 3:
            gap += 0
        ms = max(1, min(5, ms))
        what = " ".join(c["text"] for c in evidence[:2])
        findings.append(
            {
                "title": tpl["title"].format(**subs)[:160],
                "category": tpl["category"],
                "what_happened": truncate_words(what, 80, ""),
                "why_it_matters": tpl["why"].format(**subs),
                "evidence_claim_ids": ev_ids,
                "analysis_claims": [{"text": tpl["analysis"].format(**subs), "depends_on": ev_ids[:4]}],
                "recommended_action": tpl["action"].format(**subs),
                "how_to_execute": tpl["how"].format(**subs),
                "market_size": {
                    "value": ms,
                    "rationale": f"Category baseline for {cat.replace('_', ' ')} signals, adjusted for the entity's tracked share where known.",
                },
                "competitor_gap": {
                    "value": gap,
                    "rationale": "How directly the signal exposes a gap competitors have not closed.",
                },
                "effort": {"value": effort, "rationale": "Typical cross-functional effort for this kind of response."},
                "entities": [entity] if entity else [],
            }
        )
    return {"findings": findings}


# ---------------------------------------------------------------------------------------------
# Critic (stage 2) policies
# ---------------------------------------------------------------------------------------------


def _content_tokens(text: str) -> set[str]:
    return {
        t for t in tokenize(text) if t not in GENERIC_CLAIM_WORDS and not t.replace(".", "").replace(",", "").isdigit()
    }


def entailment(p: dict[str, Any]) -> dict[str, Any]:
    out = []
    for it in p.get("items", []):
        claim = it.get("claim", "")
        evidence = unwrap_untrusted(it.get("evidence", ""))
        c_tokens = _content_tokens(claim)
        e_tokens = set(tokenize(evidence))
        coverage = (len(c_tokens & e_tokens) / len(c_tokens)) if c_tokens else 1.0
        d_claim, d_ev = direction_of(claim), direction_of(evidence)
        c_nums = extract_numbers(claim)
        e_nums = extract_numbers(evidence)
        unsupported_nums = [n.raw for n in c_nums if not number_supported(n, e_nums)]
        if d_claim and d_ev and d_claim != d_ev:
            label, reason = "contradicted", "claim describes the opposite direction of change from the evidence"
        elif unsupported_nums:
            label, reason = "unsupported", f"numbers not found in evidence: {', '.join(unsupported_nums[:3])}"
        elif coverage >= 0.7:
            label, reason = "supported", f"evidence covers {coverage:.0%} of the claim's content words"
        elif coverage >= 0.45:
            label, reason = "partially_supported", f"evidence covers only {coverage:.0%} of the claim's content words"
        else:
            label, reason = "unsupported", f"evidence covers {coverage:.0%} of the claim's content words"
        out.append({"claim_id": it["claim_id"], "label": label, "reason": reason})
    return {"judgements": out}


_OVERREACH = re.compile(
    r"(?i)\b(certainly|guarantee[sd]?|definitely|always|never|all (?:customers|buyers)|everyone|will dominate|collapse|"
    r"inevitabl[ey]|proves?|undeniabl[ey]|without doubt|monopoly)\b"
)


def overreach(p: dict[str, Any]) -> dict[str, Any]:
    out = []
    for it in p.get("items", []):
        claim = it.get("claim", "")
        deps = " ".join(it.get("dependencies", []))
        dep_nums = extract_numbers(deps)
        bad_nums = [n.raw for n in extract_numbers(claim) if not number_supported(n, dep_nums)]
        if _OVERREACH.search(claim):
            label, reason = "unsupported", "uses absolute language the evidence cannot support"
        elif bad_nums:
            label, reason = "unsupported", f"introduces numbers not in its dependencies: {', '.join(bad_nums[:3])}"
        elif not it.get("dependencies"):
            label, reason = "unsupported", "no verified dependencies"
        else:
            label, reason = "supported", "stays within what the dependent claims establish"
        out.append({"claim_id": it["claim_id"], "label": label, "reason": reason})
    return {"judgements": out}


def counter_evidence(p: dict[str, Any]) -> dict[str, Any]:
    finding = p.get("finding", {})
    direction = int(finding.get("direction", 0))
    entities = [e.lower() for e in finding.get("entities", [])]
    topic = set(finding.get("topic_tokens", []))
    out = []
    for d in p.get("documents", []):
        text = unwrap_untrusted(d.get("text", ""))
        verdict, note = "unrelated", ""
        for sent in split_sentences(text):
            low = sent.lower()
            if entities and not any(e in low for e in entities):
                continue
            overlap = len(set(tokenize(sent)) & topic)
            sd = direction_of(sent)
            if overlap >= 2 and direction and sd and sd != direction:
                verdict, note = "contradicts", truncate_words(sent, 25)
                break
            if overlap >= 2 and direction and sd == direction:
                verdict, note = "supports", truncate_words(sent, 25)
        out.append({"document_id": d["id"], "verdict": verdict, "note": note})
    return {"judgements": out}


# ---------------------------------------------------------------------------------------------
# Writer and Ask
# ---------------------------------------------------------------------------------------------


def writer_prose(p: dict[str, Any]) -> dict[str, Any]:
    """Prose built only from verified claim text; whole sentences only (the brief trims to budget)."""
    f = p.get("finding", {})
    claims = p.get("claims", [])
    budget = int(p.get("word_budget", 70))
    facts = [
        re.sub(r"^Estimate(?: \([^)]*\))?:\s*", "", c["text"]) for c in claims if c.get("type") in ("fact", "estimate")
    ]
    what = facts[0] if facts else ""
    if len(facts) > 1 and len(f"{what} {facts[1]}".split()) <= max(25, int(budget * 0.5)):
        what = f"{what} {facts[1]}"
    analysis = " ".join(dict.fromkeys(p.get("analysis", []))) or f.get("why_it_matters", "")
    return {
        "headline": f.get("title", "")[:160],
        "what_happened": what,
        "why_it_matters": analysis,
        "recommended_action": f.get("recommended_action", ""),
        "how_to_execute": f.get("how_to_execute", ""),
    }


def ask_claims(p: dict[str, Any]) -> dict[str, Any]:
    aliases = p.get("aliases", {})
    question = p.get("question", "")
    focus_tokens = set(tokenize(question, keep_numbers=False)) - {"what", "which", "who", "how", "much", "many"}
    focus_tokens |= {e.lower() for e in find_entities(question, aliases)}
    claims: list[dict[str, Any]] = []
    for d in p.get("documents", []):
        d = dict(d)
        d["content"] = unwrap_untrusted(d.get("text", ""))
        if d.get("source_type") in ("reddit", "youtube"):
            sents = [s for s in split_sentences(d["content"]) if len(s.split()) >= 5 and not looks_like_injection(s)]
            sents = [s for s in sents if set(tokenize(s)) & focus_tokens]
            if sents:
                s = truncate_words(sents[0], 35, "")
                claims.append(
                    {
                        "ref": f"c{len(claims) + 1}",
                        "type": "fact",
                        "text": f'A customer post says: "{s}"',
                        "source_document_ids": [d["id"]],
                        "excerpt": s,
                        "entities": find_entities(s, aliases),
                        "tags": ["voice"],
                    }
                )
            continue
        for c in _sentence_claims(d, aliases, focus_tokens, per_doc=2, ref_start=len(claims) + 1):
            claims.append(c)
    claims = claims[: int(p.get("max_claims", 8))]
    for i, c in enumerate(claims, 1):
        c["ref"] = f"c{i}"
    return {"claims": claims, "disagreements": _disagreements(claims), "notes": "mock ask"}


def ask_answer(p: dict[str, Any]) -> dict[str, Any]:
    claims = p.get("claims", [])
    if not claims:
        return {
            "sentences": [],
            "missing": p.get("missing_hint") or "No verified evidence in the local database matches this question.",
        }
    return {"sentences": [{"text": c["text"], "claim_ids": [c["id"]]} for c in claims[:6]], "missing": ""}
