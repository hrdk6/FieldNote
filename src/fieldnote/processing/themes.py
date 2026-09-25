"""Customer-voice themes: TF-IDF + KMeans (k chosen by silhouette in 3-10), labelled by the fast LLM.

Clustering runs over the current and previous window together so each theme has a week-over-week
count. "Emerging" themes (fast growth from a small base) are flagged separately from big themes.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any

import numpy as np
from sklearn.cluster import KMeans
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics import silhouette_score
from sqlalchemy.orm import Session

from fieldnote.config import WorkspaceConfig
from fieldnote.db import repo
from fieldnote.db.models import Theme
from fieldnote.llm.base import LLMClient, LLMError
from fieldnote.llm.schemas import ThemeLabelOutput
from fieldnote.logging_setup import get_logger
from fieldnote.textutil import UNTRUSTED_NOTICE, tokenize, truncate_words, wrap_untrusted

log = get_logger(__name__)

MIN_DOCS = 8
WINDOW_DAYS = 7


def _tok(text: str) -> list[str]:
    return tokenize(text, keep_numbers=False)


def choose_k(X: Any, n_docs: int, k_min: int = 3, k_max: int = 10, seed: int = 42) -> tuple[int, np.ndarray, float]:
    """Pick k by silhouette score (cosine). Guards against tiny corpora."""
    upper = min(k_max, n_docs - 1, max(k_min, n_docs // 3))
    lower = min(k_min, upper)
    best: tuple[int, np.ndarray, float] | None = None
    for k in range(max(2, lower), max(2, upper) + 1):
        km = KMeans(n_clusters=k, n_init=10, random_state=seed)
        labels = km.fit_predict(X)
        if len(set(labels)) < 2:
            continue
        score = float(silhouette_score(X, labels, metric="cosine"))
        if best is None or score > best[2] + 1e-9:
            best = (k, labels, score)
    if best is None:
        return 1, np.zeros(n_docs, dtype=int), 0.0
    return best


def compute_themes(
    s: Session, cfg: WorkspaceConfig, run_id: int, now: datetime, llm: LLMClient | None
) -> dict[str, Any]:
    cur_start = now - timedelta(days=WINDOW_DAYS)
    prev_start = now - timedelta(days=2 * WINDOW_DAYS)
    rows = repo.voice_rows(s, cfg.workspace, since=prev_start, until=now)
    if len(rows) < MIN_DOCS:
        return {"status": "skipped", "reason": f"only {len(rows)} tagged posts (need {MIN_DOCS})", "themes": 0}
    texts = [f"{d.title}\n{d.content}" for _, d in rows]
    ts = [(d.published_at or d.fetched_at) for _, d in rows]
    is_current = [t > cur_start for t in ts]
    vec = TfidfVectorizer(
        tokenizer=_tok,
        lowercase=False,
        token_pattern=None,
        ngram_range=(1, 2),
        min_df=2 if len(texts) >= 20 else 1,
        max_df=0.7,
        max_features=4000,
        sublinear_tf=True,
    )
    try:
        X = vec.fit_transform(texts)
    except ValueError as exc:  # empty vocabulary
        return {"status": "skipped", "reason": f"vectoriser: {exc}", "themes": 0}
    if X.shape[1] < 3:
        return {"status": "skipped", "reason": "vocabulary too small", "themes": 0}
    k, labels, sil = choose_k(X, len(texts))
    terms = np.array(vec.get_feature_names_out())
    clusters: list[dict[str, Any]] = []
    for c in range(k):
        idx = [i for i, lab in enumerate(labels) if lab == c]
        if not idx:
            continue
        centroid = np.asarray(X[idx].mean(axis=0)).ravel()
        order = np.argsort(-centroid)[:8]
        top = [str(terms[j]) for j in order if centroid[j] > 0]
        # Samples closest to the centroid, current window first.
        sims = np.asarray(X[idx] @ centroid).ravel()
        by_sim = [idx[j] for j in np.argsort(-sims, kind="stable")]
        pos = {doc_i: n for n, doc_i in enumerate(by_sim)}
        ranked = sorted(by_sim, key=lambda i: (not is_current[i], pos[i]))
        cur = [i for i in idx if is_current[i]]
        prev = [i for i in idx if not is_current[i]]
        focus = cur or idx
        tags = [rows[i][0] for i in focus]
        aspects = Counter(t.aspect for t in tags)
        ents = Counter(e for t in tags for e in (t.entities or []))
        clusters.append(
            {
                "cluster_id": c,
                "top_terms": top,
                "samples": [truncate_words(texts[i], 60) for i in ranked[:8]],
                "sample_ids": [rows[i][1].id for i in ranked[:8]],
                "dominant_aspect": aspects.most_common(1)[0][0] if aspects else "other",
                "size": len(cur),
                "size_prev": len(prev),
                "sentiment": float(np.mean([t.sentiment for t in tags])) if tags else 0.0,
                "aspects": dict(aspects),
                "entities": dict(ents),
                "n_tagged": len(tags),
            }
        )
    labels_by_id: dict[int, str] = {}
    if llm is not None:
        try:
            out = llm.structured(
                task="theme_labeling",
                system=(
                    "Give each cluster of customer posts a short, neutral, descriptive label (max 6 words). Do not "
                    f"invent facts or name people. {UNTRUSTED_NOTICE}"
                ),
                prompt="\n\n".join(
                    f"Cluster {c['cluster_id']} (top terms: {', '.join(c['top_terms'])}):\n"
                    + "\n".join(f"- {wrap_untrusted(smp)}" for smp in c["samples"])
                    for c in clusters
                ),
                schema=ThemeLabelOutput,
                tier="fast",
                payload={
                    "clusters": [
                        {
                            "cluster_id": c["cluster_id"],
                            "top_terms": c["top_terms"],
                            "dominant_aspect": c["dominant_aspect"],
                            "sentiment": c["sentiment"],
                            "entities": c["entities"],
                            "n": c["n_tagged"],
                        }
                        for c in clusters
                    ]
                },
            )
            labels_by_id = {lab.cluster_id: lab.label for lab in out.labels}
        except LLMError as exc:
            log.info("theme labelling fell back to top terms: %s", exc)
    total_current = sum(1 for x in is_current if x) or 1
    total_prev = sum(1 for x in is_current if not x) or 1
    stored = 0
    for cl in clusters:
        size, prev = cl["size"], cl["size_prev"]
        wow = ((size - prev) / prev) if prev else None
        # Emerging = fast growth in *share of voice* from a small base (adjusts for overall volume changes).
        share_cur, share_prev = size / total_current, prev / total_prev
        emerging = size >= 3 and share_cur <= 0.2 and (prev == 0 or share_cur >= 2 * share_prev)
        s.add(
            Theme(
                workspace=cfg.workspace,
                run_id=run_id,
                label=labels_by_id.get(cl["cluster_id"])
                or ", ".join(cl["top_terms"][:3])
                or f"Theme {cl['cluster_id']}",
                keywords=cl["top_terms"],
                size=size,
                size_prev=prev,
                wow_change=round(wow, 3) if wow is not None else None,
                is_emerging=emerging,
                sentiment_mean=round(cl["sentiment"], 3),
                sample_document_ids=cl["sample_ids"],
                aspect_distribution=cl["aspects"],
                entity_distribution=cl["entities"],
            )
        )
        stored += 1
    s.flush()
    return {"status": "ok", "themes": stored, "k": k, "silhouette": round(sil, 3), "documents": len(texts)}
