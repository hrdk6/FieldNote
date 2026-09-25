"""YouTube collector via the Data API v3 (plain REST), quota-aware.

search.list costs 100 units, commentThreads.list costs 1 unit. The per-run budget
(``sources.youtube.quota_units_per_run``) caps both. Comment author names are never stored.
"""

from __future__ import annotations

import json
from datetime import datetime

import requests
from dateutil import parser as dtparser

from fieldnote.collectors.base import Collector
from fieldnote.config import WorkspaceConfig
from fieldnote.domain import CollectedDocument, CollectorResult
from fieldnote.logging_setup import get_logger
from fieldnote.processing.clean import sanitize_text
from fieldnote.textutil import find_entities, normalize_ws

log = get_logger(__name__)

API = "https://www.googleapis.com/youtube/v3"
SEARCH_COST = 100
COMMENTS_COST = 1


class YoutubeCollector(Collector):
    name = "youtube"

    def available(self) -> tuple[bool, str]:
        if not self.settings.has_youtube:
            return False, "YOUTUBE_API_KEY not set; YouTube collection skipped"
        return True, "ready"

    def _api(self, endpoint: str, params: dict[str, str | int]) -> dict:
        assert self.http is not None
        params = {**params, "key": self.settings.youtube_api_key or ""}
        # Official API endpoint: robots.txt governs crawlers, not API clients, so no robots check here.
        res = self.http.get(f"{API}/{endpoint}", params=params, conditional=False, check_robots=False)
        if res.status == 403:
            raise PermissionError(f"YouTube API refused the request (quota or key): {res.text[:200]}")
        if not res.ok:
            raise requests.HTTPError(f"YouTube API HTTP {res.status}")
        return json.loads(res.text or "{}")

    def _collect(self, cfg: WorkspaceConfig, since: datetime) -> CollectorResult:
        ycfg = cfg.sources.youtube
        if not ycfg.search_terms or ycfg.max_videos == 0:
            return CollectorResult(name=self.name, status="skipped", message="no YouTube search terms configured")
        budget = ycfg.quota_units_per_run
        used = 0
        aliases = cfg.entity_aliases()
        region = cfg.news_locale().gl
        videos: dict[str, dict[str, str]] = {}
        errors: list[str] = []
        per_term = max(1, ycfg.max_videos // len(ycfg.search_terms))
        for term in ycfg.search_terms:
            if used + SEARCH_COST > budget or len(videos) >= ycfg.max_videos:
                break
            try:
                data = self._api(
                    "search",
                    {
                        "part": "snippet",
                        "q": term,
                        "type": "video",
                        "maxResults": min(50, per_term),
                        "order": "date",
                        "publishedAfter": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "regionCode": region,
                        "relevanceLanguage": (cfg.language_hints or ["en"])[0].split("-")[0],
                    },
                )
                used += SEARCH_COST
            except Exception as exc:
                errors.append(f"search '{term}': {exc}")
                break
            for item in data.get("items", []):
                vid = item.get("id", {}).get("videoId")
                if vid and vid not in videos:
                    videos[vid] = {"title": normalize_ws(item["snippet"].get("title", ""))}
        docs: list[CollectedDocument] = []
        for vid, info in list(videos.items())[: ycfg.max_videos]:
            if used + COMMENTS_COST > budget:
                errors.append("quota budget reached")
                break
            try:
                data = self._api(
                    "commentThreads",
                    {
                        "part": "snippet",
                        "videoId": vid,
                        "maxResults": min(100, ycfg.max_comments_per_video),
                        "order": "relevance",
                        "textFormat": "plainText",
                    },
                )
                used += COMMENTS_COST
            except Exception as exc:
                errors.append(f"comments {vid}: {type(exc).__name__}")
                continue
            for th in data.get("items", []):
                top = th.get("snippet", {}).get("topLevelComment", {})
                sn = top.get("snippet", {})
                published = dtparser.isoparse(sn["publishedAt"]).replace(tzinfo=None) if sn.get("publishedAt") else None
                if published and published <= since:
                    continue
                text, flags = sanitize_text(sn.get("textOriginal") or sn.get("textDisplay") or "")
                if len(text) < 15:
                    continue
                cid = top.get("id", "")
                url = f"https://www.youtube.com/watch?v={vid}&lc={cid}"
                docs.append(
                    CollectedDocument(
                        source_type="youtube",
                        source_name=f"YouTube: {info['title'][:120]}",
                        url=url,
                        canonical_url=url,
                        title=f"Comment on: {info['title'][:200]}",
                        content=text,
                        published_at=published,
                        fetched_at=self.now,
                        metadata={
                            "video_id": vid,
                            "video_title": info["title"],
                            "like_count": int(sn.get("likeCount", 0) or 0),
                            "entities": find_entities(f"{info['title']}\n{text}", aliases),
                            **flags,
                        },
                    )
                )
        status = "ok" if not errors else ("partial" if docs else "failed")
        return CollectorResult(
            name=self.name,
            documents=docs,
            status=status,
            message="; ".join(errors[:5]),
            stats={"videos": len(videos), "comments": len(docs), "quota_units_used": used},
        )
