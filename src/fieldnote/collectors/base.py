"""Collector interface and the polite HTTP client every collector uses.

Compliance rules enforced here (see docs/sources-and-compliance.md):
* robots.txt is checked (urllib.robotparser) before any page fetch; disallowed URLs are skipped.
* An honest User-Agent: ``FieldNoteBot/1.0 (+<contact>)``.
* Per-host rate limiting (``per_host_delay_seconds``).
* Conditional requests (ETag / Last-Modified) where the server supports them.
* Retries with exponential backoff on 429/5xx/connection errors, honouring Retry-After.
* Hosts with a published usage policy (e.g. the GDELT API: one request per 5 seconds) get slower pacing.
* SSRF guard: URLs that resolve to private, loopback, link-local or otherwise non-public addresses are
  refused, and every redirect hop is re-checked (``FIELDNOTE_ALLOW_PRIVATE_URLS=1`` disables this for
  intranet deployments).
"""

from __future__ import annotations

import contextlib
import ipaddress
import random
import socket
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib import robotparser
from urllib.parse import urljoin, urlsplit

import requests
from sqlalchemy import select

from fieldnote.config import Settings, WorkspaceConfig
from fieldnote.db.engine import Database, utcnow
from fieldnote.db.models import HttpCache
from fieldnote.domain import CollectorResult
from fieldnote.logging_setup import get_logger

log = get_logger(__name__)

ROBOTS_TOKEN = "FieldNoteBot"  # noqa: S105 - a robots.txt user-agent token, not a secret
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_REDIRECTS = 5
# Hosts whose usage policy needs slower pacing than the workspace default (seconds between requests).
HOST_MIN_DELAY: dict[str, float] = {"api.gdeltproject.org": 6.0}
# Minimum pause before retrying a throttled/failed request to these hosts.
HOST_RETRY_FLOOR: dict[str, float] = {"api.gdeltproject.org": 20.0}
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".lan", ".intranet")


class RobotsDisallowedError(Exception):
    pass


class UnsafeUrlError(requests.RequestException):
    """The URL is not a public http(s) address (private, loopback, link-local, metadata, ...)."""


def _hostname(netloc_or_host: str) -> str:
    host = netloc_or_host.rsplit("@", 1)[-1]
    host = host[1:].split("]", 1)[0] if host.startswith("[") else host.split(":", 1)[0]
    return host.lower().rstrip(".")


@dataclass
class FetchResult:
    url: str
    status: int
    text: str = ""
    content: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    not_modified: bool = False
    final_url: str = ""
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 or self.not_modified


class PoliteHttpClient:
    def __init__(
        self,
        settings: Settings,
        *,
        per_host_delay: float = 2.0,
        timeout: float = 20.0,
        max_retries: int = 3,
        backoff_base: float = 1.5,
        db: Database | None = None,
        session: requests.Session | None = None,
        resolver: Any = None,
    ) -> None:
        self.user_agent = settings.user_agent
        self.allow_private = settings.allow_private_urls
        self._resolver = resolver or socket.getaddrinfo
        self._public_hosts: set[str] = set()
        self.per_host_delay = per_host_delay
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.db = db
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.user_agent,
                "Accept-Language": "en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/rss+xml,application/atom+xml,application/json;q=0.8,*/*;q=0.5",
            }
        )
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._robots_status: dict[str, str] = {}
        self._last_hit: dict[str, float] = {}
        self._lock = threading.Lock()
        self.stats: dict[str, int] = {"requests": 0, "robots_blocked": 0, "not_modified": 0, "errors": 0}

    # ---- robots.txt -------------------------------------------------------------------------
    def _robots_for(self, url: str) -> robotparser.RobotFileParser | None:
        self.check_public(url)
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base in self._robots:
            return self._robots[base]
        rp = robotparser.RobotFileParser()
        robots_url = f"{base}/robots.txt"
        try:
            self._throttle(parts.netloc)
            resp = self._send(robots_url, {}, None, check_robots=False)
            self.stats["requests"] += 1
            if resp.status_code in (401, 403):
                rp.disallow_all = True  # type: ignore[attr-defined]
                self._robots_status[base] = f"robots.txt returned {resp.status_code}: treating as disallow-all"
            elif 400 <= resp.status_code < 500:
                rp.allow_all = True  # type: ignore[attr-defined]
                self._robots_status[base] = "no robots.txt (allow)"
            elif resp.status_code >= 500:
                rp.disallow_all = True  # type: ignore[attr-defined]
                self._robots_status[base] = f"robots.txt unreachable ({resp.status_code}): treating as disallow-all"
            else:
                rp.parse(resp.text.splitlines())
                self._robots_status[base] = "robots.txt parsed"
        except requests.RequestException as exc:
            log.warning("could not fetch %s (%s); skipping host to stay compliant", robots_url, type(exc).__name__)
            rp.disallow_all = True  # type: ignore[attr-defined]
            self._robots_status[base] = f"robots.txt fetch failed ({type(exc).__name__}): treating as disallow-all"
        self._robots[base] = rp
        return rp

    def allowed(self, url: str) -> bool:
        rp = self._robots_for(url)
        if rp is None:
            return True
        return rp.can_fetch(ROBOTS_TOKEN, url) and rp.can_fetch(self.user_agent, url)

    def robots_status(self, url: str) -> str:
        parts = urlsplit(url)
        return self._robots_status.get(f"{parts.scheme}://{parts.netloc}", "unchecked")

    # ---- SSRF guard ---------------------------------------------------------------------------
    def check_public(self, url: str) -> None:
        """Raise :class:`UnsafeUrlError` unless ``url`` is http(s) on a publicly routable host."""
        if self.allow_private:
            return
        try:
            parts = urlsplit(url)
        except ValueError as exc:
            raise UnsafeUrlError(f"malformed URL {url!r}") from exc
        if parts.scheme not in ("http", "https"):
            raise UnsafeUrlError(f"only http(s) URLs are fetched, not {parts.scheme or 'none'!r}")
        host = _hostname(parts.netloc)
        if not host:
            raise UnsafeUrlError(f"URL has no host: {url!r}")
        if host in self._public_hosts:
            return
        if host == "localhost" or host.endswith(_BLOCKED_SUFFIXES):
            raise UnsafeUrlError(f"{host} is a local/internal host name")
        try:
            addrs = [ipaddress.ip_address(host)]
        except ValueError:
            try:
                infos = self._resolver(host, None)
            except (OSError, UnicodeError):
                # Unresolvable: the request itself will fail, so nothing internal can be reached.
                return
            addrs = []
            for info in infos:
                with contextlib.suppress(ValueError):
                    addrs.append(ipaddress.ip_address(str(info[4][0]).split("%")[0]))
        for addr in addrs:
            if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
                addr = addr.ipv4_mapped
            if not addr.is_global:
                raise UnsafeUrlError(f"{host} resolves to a non-public address; refusing to fetch")
        self._public_hosts.add(host)

    def _send(
        self, url: str, headers: dict[str, str], params: dict[str, Any] | None, *, check_robots: bool
    ) -> requests.Response:
        """GET without automatic redirects; every hop is re-checked (public address, robots.txt)."""
        resp = self.session.get(url, headers=headers, params=params, timeout=self.timeout, allow_redirects=False)
        current = url
        for _hop in range(MAX_REDIRECTS):
            location = resp.headers.get("Location") if getattr(resp, "is_redirect", False) else None
            if not location:
                return resp
            current = urljoin(getattr(resp, "url", None) or current, location)
            self.check_public(current)
            if check_robots and not self.allowed(current):
                self.stats["robots_blocked"] += 1
                raise RobotsDisallowedError(f"robots.txt disallows redirect target {current}")
            self._throttle(urlsplit(current).netloc)
            resp = self.session.get(current, headers=headers, timeout=self.timeout, allow_redirects=False)
        if getattr(resp, "is_redirect", False):
            raise requests.TooManyRedirects(f"more than {MAX_REDIRECTS} redirects from {url}")
        return resp

    # ---- rate limiting ------------------------------------------------------------------------
    def _min_delay(self, host: str) -> float:
        return max(self.per_host_delay, HOST_MIN_DELAY.get(_hostname(host), 0.0))

    def _throttle(self, host: str) -> None:
        with self._lock:
            last = self._last_hit.get(host)
            now = time.monotonic()
            if last is not None:
                wait = self._min_delay(host) - (now - last)
                if wait > 0:
                    time.sleep(wait)
            self._last_hit[host] = time.monotonic()

    # ---- conditional request cache --------------------------------------------------------
    def _cache_get(self, url: str) -> HttpCache | None:
        if self.db is None:
            return None
        with self.db.session() as s:
            return s.scalar(select(HttpCache).where(HttpCache.url == url))

    def _cache_put(self, url: str, resp: requests.Response, body: str) -> None:
        if self.db is None:
            return
        etag = resp.headers.get("ETag")
        lm = resp.headers.get("Last-Modified")
        if not etag and not lm:
            return
        with self.db.session() as s:
            row = s.scalar(select(HttpCache).where(HttpCache.url == url))
            if row is None:
                row = HttpCache(url=url[:1000])
                s.add(row)
            row.etag = etag
            row.last_modified = lm
            row.status = resp.status_code
            row.body = body[:2_000_000]
            row.fetched_at = utcnow()

    # ---- fetching ---------------------------------------------------------------------------
    def get(
        self,
        url: str,
        *,
        conditional: bool = True,
        check_robots: bool = True,
        params: dict[str, Any] | None = None,
        use_cached_body_on_304: bool = False,
    ) -> FetchResult:
        self.check_public(url)
        if check_robots and not self.allowed(url):
            self.stats["robots_blocked"] += 1
            raise RobotsDisallowedError(f"robots.txt disallows {url}")
        headers: dict[str, str] = {}
        cached = self._cache_get(url) if conditional and params is None else None
        if cached is not None:
            if cached.etag:
                headers["If-None-Match"] = cached.etag
            if cached.last_modified:
                headers["If-Modified-Since"] = cached.last_modified
        host = urlsplit(url).netloc
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle(host)
            start = time.perf_counter()
            try:
                resp = self._send(url, headers, params, check_robots=check_robots)
            except (UnsafeUrlError, requests.TooManyRedirects):
                self.stats["errors"] += 1
                raise
            except requests.RequestException as exc:
                last_exc = exc
                self.stats["errors"] += 1
                if attempt < self.max_retries:
                    self._sleep_backoff(attempt, None, host)
                continue
            self.stats["requests"] += 1
            elapsed = int((time.perf_counter() - start) * 1000)
            if resp.status_code == 304 and cached is not None:
                self.stats["not_modified"] += 1
                return FetchResult(
                    url=url,
                    status=304,
                    not_modified=True,
                    final_url=resp.url,
                    elapsed_ms=elapsed,
                    text=cached.body if use_cached_body_on_304 else "",
                    headers=dict(resp.headers),
                )
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                self._sleep_backoff(attempt, resp.headers.get("Retry-After"), host)
                continue
            text = ""
            ctype = resp.headers.get("Content-Type", "")
            if "text" in ctype or "xml" in ctype or "json" in ctype or "html" in ctype or not ctype:
                resp.encoding = resp.encoding or resp.apparent_encoding
                text = resp.text
            if 200 <= resp.status_code < 300 and params is None:
                self._cache_put(url, resp, text)
            return FetchResult(
                url=url,
                status=resp.status_code,
                text=text,
                content=resp.content,
                headers=dict(resp.headers),
                final_url=resp.url,
                elapsed_ms=elapsed,
            )
        raise requests.ConnectionError(f"giving up on {url} after {self.max_retries + 1} attempts: {last_exc}")

    def _sleep_backoff(self, attempt: int, retry_after: str | None, host: str = "") -> None:
        delay = self.backoff_base * (2**attempt) + random.uniform(0, 0.5)
        delay = max(delay, HOST_RETRY_FLOOR.get(_hostname(host), 0.0))
        if retry_after:
            with contextlib.suppress(ValueError):
                delay = max(delay, min(float(retry_after), 60.0))
        time.sleep(delay)


class Collector(ABC):
    """All collectors implement ``collect(workspace_cfg, since) -> CollectorResult``.

    Collectors never raise: failures are captured in the result so one source can't stop the run.
    """

    name: str = "collector"

    def __init__(self, settings: Settings, http: PoliteHttpClient | None = None, now: datetime | None = None) -> None:
        self.settings = settings
        self.http = http
        self.now = now or utcnow()

    @abstractmethod
    def _collect(self, cfg: WorkspaceConfig, since: datetime) -> CollectorResult: ...

    def available(self) -> tuple[bool, str]:
        return True, "ready"

    def collect(self, cfg: WorkspaceConfig, since: datetime) -> CollectorResult:
        ok, why = self.available()
        if not ok:
            log.warning("%s collector skipped: %s", self.name, why)
            return CollectorResult(name=self.name, status="skipped", message=why)
        started = time.perf_counter()
        try:
            result = self._collect(cfg, since)
        except Exception as exc:
            log.exception("%s collector failed", self.name)
            return CollectorResult(name=self.name, status="failed", message=f"{type(exc).__name__}: {exc}")
        result.stats.setdefault("seconds", round(time.perf_counter() - started, 2))
        return result
