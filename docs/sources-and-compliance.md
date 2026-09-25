# Sources and compliance

These rules are enforced in code (`collectors/base.py` and the collectors) and are not configurable off.

## Rules for every collector

| Rule | How it is enforced |
|---|---|
| Check robots.txt before fetching any page or feed | `PoliteHttpClient.allowed()` uses `urllib.robotparser` per host (cached). Disallowed URLs raise `RobotsDisallowedError`, are skipped and counted in run stats. A robots.txt that returns 401/403 or 5xx, or cannot be fetched, is treated as disallow-all; 404 means allow. The most specific user-agent group applies (RFC 9309). |
| Honest User-Agent | `FieldNoteBot/1.0 (+<FIELDNOTE_CONTACT_EMAIL>)` on every request. `fieldnote doctor` warns if no contact is set. |
| Per-host rate limit | `limits.per_host_delay_seconds` (default 2 s) between requests to the same host, including robots.txt. |
| Conditional requests | ETag / Last-Modified are stored in `http_cache` and sent as `If-None-Match` / `If-Modified-Since`; 304 responses are not re-processed. |
| Retries with backoff | 429/5xx and connection errors are retried with exponential backoff, honouring `Retry-After` (capped at 60 s). |
| No logins, paywalls or CAPTCHAs | FieldNote never submits credentials to websites and has no CAPTCHA handling. Paywalled articles yield only the public summary. |
| Official APIs for social platforms | Reddit via PRAW (read-only), YouTube via the Data API v3. Without credentials those collectors are skipped with a clear message. |
| No author identities | Reddit author names and YouTube author names/channels are never read into documents or stored. Posts are identified by permalink. FieldNote never profiles individuals; outputs say "a customer post". |
| Store for analysis, quote sparingly | Full text is stored locally for analysis only. Outputs quote at most ~25 words per source and always link to it. |
| Retention | `fieldnote purge` deletes social posts older than `retention_days.posts` (default 180) and other documents older than `retention_days.documents` (default 365). The daily workflow runs it. |

## Source-specific notes

* **GDELT news search.** `sources.news.search_queries` run as one combined query per workspace run against
  the GDELT DOC 2.0 API, an open research API that needs no key. GDELT asks for at most one request every
  5 seconds; FieldNote spaces requests to that host at 6 seconds and waits at least 20 seconds before
  retrying a rate-limited request. Articles are then fetched from their publishers under the normal
  robots.txt and rate-limit rules, and hits that mention none of the workspace's entities, aliases,
  keywords or search phrases are dropped. GDELT can rate-limit or slow down; runs then continue with the
  other sources and report the news search as degraded.
* **Private addresses.** The HTTP client refuses URLs that are not http(s) or that resolve to private,
  loopback, link-local or otherwise non-public addresses (including cloud metadata endpoints), and it
  re-checks every redirect hop. This matters once a hosted dashboard lets people type URLs. Set
  `FIELDNOTE_ALLOW_PRIVATE_URLS=1` only for an intranet deployment that needs internal sources.
* **Workspace builder.** Model-proposed URLs are candidates, never trusted: each one is fetched under the
  rules above and kept only if it works (see `docs/adding-a-workspace.md`).

* **Google News RSS.** Queries are built region-aware (`hl`, `gl`, `ceid`). News.google.com's robots.txt
  currently disallows `/rss` for crawlers, so these are skipped; `fieldnote doctor` shows the status.
  Google News item links are JavaScript redirects, so even where allowed, only title and summary are
  stored for those items.
* **RSS/Atom feeds.** Articles are fetched for full text (trafilatura) only where robots.txt allows the
  article URL; otherwise the feed summary is used.
* **Competitor pages.** Only pages listed in the workspace are fetched. `render: true` uses headless
  Chromium via the optional Playwright extra, after the same robots and rate-limit checks.
* **Reddit.** Uses the official API with your app credentials (`REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`,
  `REDDIT_USER_AGENT`). Respect Reddit's API terms and rate limits for your app type.
* **YouTube.** Uses the Data API v3 with `YOUTUBE_API_KEY`. `search.list` costs 100 quota units and
  `commentThreads.list` 1 unit; `sources.youtube.quota_units_per_run` caps usage per run.

## Market, registration and sales data

Aggregates published by public dashboards and industry bodies (for example vehicle registration
portals or industry association releases) must be obtained **manually or through the publisher's
sanctioned method** (downloads, official APIs, licensed feeds), respecting their terms of use.
FieldNote does not automate against dashboards whose terms forbid it.

To use such data:

1. Download the export yourself (CSV) or obtain an API endpoint you are permitted to query.
2. Save the CSV under `data/` and add a `csv_file` connector with a column `mapping`, the `unit`, and
   the `source_url` it came from (shown in outputs as the source). Lines starting with `#` are ignored,
   so you can record provenance at the top of the file.
3. For JSON APIs you are allowed to query, use `http_json` with `records_path` and a dotted mapping.
   robots.txt is honoured for these requests too.

New connector types can be added by dropping a module in `src/fieldnote/collectors/connectors/` with a
class decorated `@register("your_type")`.

## Verification of shipped URLs

Every feed and page URL in the shipped workspace files was checked with `fieldnote doctor` during the
build (reachable, robots.txt allows FieldNoteBot, feeds contain entries). URLs that failed were dropped;
see `docs/assumptions.md` for the list. Re-run `fieldnote doctor` periodically: sites change.
