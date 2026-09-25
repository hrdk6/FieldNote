"""Reddit collector via the official API (PRAW), read-only.

Stores post text, permalink, date, score and comment count. Author usernames and profile data are
never stored. Without credentials the collector is skipped with a clear warning.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fieldnote.collectors.base import Collector
from fieldnote.config import WorkspaceConfig
from fieldnote.domain import CollectedDocument, CollectorResult
from fieldnote.logging_setup import get_logger
from fieldnote.processing.clean import sanitize_text
from fieldnote.textutil import find_entities

log = get_logger(__name__)


def submission_to_document(sub: Any, aliases: dict[str, str], now: datetime) -> CollectedDocument:
    """Convert a PRAW submission to a document. Deliberately ignores ``sub.author``."""
    created = datetime.fromtimestamp(float(sub.created_utc), UTC).replace(tzinfo=None)
    title = str(getattr(sub, "title", "") or "")
    body = str(getattr(sub, "selftext", "") or "")
    text, flags = sanitize_text(f"{title}\n\n{body}".strip())
    permalink = f"https://www.reddit.com{sub.permalink}"
    return CollectedDocument(
        source_type="reddit",
        source_name=f"r/{sub.subreddit.display_name}",
        url=permalink,
        canonical_url=permalink,
        title=title[:500],
        content=text,
        published_at=created,
        fetched_at=now,
        metadata={
            "score": int(getattr(sub, "score", 0) or 0),
            "num_comments": int(getattr(sub, "num_comments", 0) or 0),
            "subreddit": sub.subreddit.display_name,
            "entities": find_entities(text, aliases),
            **flags,
        },
    )


class RedditCollector(Collector):
    name = "reddit"

    def available(self) -> tuple[bool, str]:
        if not self.settings.has_reddit:
            return False, "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET not set; Reddit collection skipped"
        try:
            import praw  # noqa: F401
        except ImportError:
            return False, "praw is not installed"
        return True, "ready"

    def _client(self) -> Any:
        import praw

        ua = self.settings.reddit_user_agent or f"{self.settings.user_agent} (read-only research)"
        reddit = praw.Reddit(
            client_id=self.settings.reddit_client_id,
            client_secret=self.settings.reddit_client_secret,
            user_agent=ua,
            check_for_async=False,
            ratelimit_seconds=60,
        )
        reddit.read_only = True
        return reddit

    def _collect(self, cfg: WorkspaceConfig, since: datetime) -> CollectorResult:
        rcfg = cfg.sources.reddit
        if not rcfg.subreddits:
            return CollectorResult(name=self.name, status="skipped", message="no subreddits configured")
        reddit = self._client()
        aliases = cfg.entity_aliases()
        cap = rcfg.max_posts_per_run
        per_listing = max(10, cap // max(1, len(rcfg.subreddits) * (2 + len(rcfg.search_terms))))
        seen: set[str] = set()
        docs: list[CollectedDocument] = []
        errors: list[str] = []

        def take(listing: Any) -> None:
            for sub in listing:
                if len(docs) >= cap:
                    return
                if sub.id in seen:
                    continue
                created = datetime.fromtimestamp(float(sub.created_utc), UTC).replace(tzinfo=None)
                if created <= since:
                    continue
                seen.add(sub.id)
                docs.append(submission_to_document(sub, aliases, self.now))

        for name in rcfg.subreddits:
            try:
                sr = reddit.subreddit(name)
                take(sr.new(limit=per_listing))
                take(sr.top(time_filter="week", limit=per_listing))
            except Exception as exc:
                errors.append(f"r/{name}: {type(exc).__name__}")
        if rcfg.search_terms and len(docs) < cap:
            multi = reddit.subreddit("+".join(rcfg.subreddits))
            for term in rcfg.search_terms:
                try:
                    take(multi.search(term, sort="new", time_filter="week", limit=per_listing))
                except Exception as exc:
                    errors.append(f"search '{term}': {type(exc).__name__}")
        status = "ok" if not errors else ("partial" if docs else "failed")
        return CollectorResult(
            name=self.name,
            documents=docs,
            status=status,
            message="; ".join(errors[:5]),
            stats={"posts": len(docs), "errors": len(errors)},
        )
