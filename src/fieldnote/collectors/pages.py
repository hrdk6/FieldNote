"""Competitor page collector: fetch, extract main text and structured hints (prices, headings).

Snapshots and diffs are handled by ``processing.change_detection``; this collector only fetches.
Pages flagged ``render: true`` use Playwright when the optional ``render`` extra is installed.
"""

from __future__ import annotations

from datetime import datetime

import requests

from fieldnote.collectors.base import Collector, RobotsDisallowedError
from fieldnote.config import PageConfig, WorkspaceConfig
from fieldnote.domain import CollectedDocument, CollectorResult
from fieldnote.logging_setup import get_logger
from fieldnote.processing.clean import extract_main_text, sanitize_text, structured_hints

log = get_logger(__name__)


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


class PagesCollector(Collector):
    name = "pages"

    def _render(self, page: PageConfig) -> str | None:
        """Render with headless Chromium. Robots and rate limits are enforced before rendering."""
        assert self.http is not None
        if not self.http.allowed(page.url):
            raise RobotsDisallowedError(page.url)
        from urllib.parse import urlsplit

        from playwright.sync_api import sync_playwright

        self.http._throttle(urlsplit(page.url).netloc)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(user_agent=self.http.user_agent)
                pg = ctx.new_page()
                pg.goto(page.url, wait_until="networkidle", timeout=int(self.http.timeout * 1000))
                return str(pg.content())
            finally:
                browser.close()

    def _collect(self, cfg: WorkspaceConfig, since: datetime) -> CollectorResult:
        assert self.http is not None
        docs: list[CollectedDocument] = []
        errors: list[str] = []
        blocked = 0
        rendered = 0
        unchanged = 0
        can_render = playwright_available()
        for comp in cfg.competitors:
            for page in comp.pages:
                html: str | None = None
                try:
                    if page.render and can_render:
                        html = self._render(page)
                        rendered += 1
                    else:
                        if page.render:
                            log.warning(
                                "page %s wants render:true but Playwright is not installed; using plain fetch", page.url
                            )
                        res = self.http.get(page.url, conditional=True, use_cached_body_on_304=True)
                        if res.not_modified:
                            unchanged += 1
                        if not res.ok:
                            errors.append(f"{page.url}: HTTP {res.status}")
                            continue
                        html = res.text
                except RobotsDisallowedError:
                    blocked += 1
                    log.info("robots.txt disallows %s; skipped", page.url)
                    continue
                except (requests.RequestException, RuntimeError) as exc:
                    errors.append(f"{page.url}: {type(exc).__name__}")
                    continue
                except Exception as exc:  # playwright errors
                    errors.append(f"{page.url}: {type(exc).__name__}")
                    continue
                if not html:
                    continue
                text = extract_main_text(html, url=page.url)
                text, flags = sanitize_text(text)
                hints = structured_hints(html, text, page.selector)
                docs.append(
                    CollectedDocument(
                        source_type="page",
                        source_name=f"{comp.name} ({page.kind})",
                        url=page.url,
                        canonical_url=page.url,
                        title=(hints["headings"][0] if hints["headings"] else f"{comp.name} {page.kind} page"),
                        content=text,
                        fetched_at=self.now,
                        published_at=self.now,
                        is_primary=True,
                        metadata={
                            "competitor": comp.name,
                            "page_kind": page.kind,
                            "page_url": page.url,
                            "structured": hints,
                            "entities": [comp.name],
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
            stats={
                "pages": sum(len(c.pages) for c in cfg.competitors),
                "fetched": len(docs),
                "robots_blocked": blocked,
                "rendered": rendered,
                "not_modified": unchanged,
                "errors": len(errors),
            },
        )
