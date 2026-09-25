# Assumptions and defaults

Decisions made where the brief left room, recorded so they can be revisited.

## Runtime and storage

* **Python 3.11+** is supported; development, CI and Docker use 3.12 (mypy targets 3.12 because current
  numpy stubs use 3.12 syntax).
* **Databases.** `DATABASE_URL` always wins. Otherwise live runs use `data/fieldnote.db` and offline /
  demo runs use `data/demo.db`, so fixture data never mixes with live data. The dashboard opens the live
  database if it contains runs, otherwise the demo database (override with `DATABASE_URL` or
  `fieldnote dashboard --demo`).
* **Timestamps** are stored as naive UTC; dates shown to people use the workspace timezone.
* **Schema management** is a versioned `create_all` plus additive column migration (no Alembic), which
  suits a single-service tool.
* **LLM output cache and traces** are buffered in memory and flushed between pipeline stages, so they
  never contend with an open SQLite write transaction.

## LLM

* Models from env: `FIELDNOTE_MODEL` (default `claude-sonnet-5`) and `FIELDNOTE_FAST_MODEL`
  (default `claude-haiku-4-5-20251001`) for tagging, classification, routing and extraction.
* `FIELDNOTE_LLM` = `auto` (default: chain every provider with a key in the order Anthropic, Gemini,
  NVIDIA; mock when there is no key or the run is offline), `mock`, a single provider (`anthropic`,
  `gemini`, `nvidia`) or a comma-separated chain such as `gemini,nvidia`. Offline runs use the MockClient
  in `auto` mode only.
* Free-tier defaults: Gemini `gemini-2.5-flash` (main) / `gemini-3.5-flash-lite` (fast) at 8 requests per
  minute; NVIDIA `nvidia/nemotron-3-super-120b-a12b` for both tiers at 30 requests per minute. Both are reached
  through their OpenAI-compatible endpoints with plain HTTPS (no extra SDK). Free-tier limits change and
  vary per account, so model names and `*_RPM` are env-configurable and `fieldnote doctor --check-llm`
  verifies the models exist for your key. Free-tier calls are recorded at $0.
* Free tiers may use submitted data to improve the provider's models (check their terms); FieldNote sends
  collected public content and, if you use them, your meeting notes.
* Structured output uses forced tool use; if a model rejects forced `tool_choice`, the client retries
  with `auto` plus an explicit instruction and checks the tool was called. Validation failures are
  retried twice with the error fed back.
* Cost accounting uses a built-in price table (USD per million tokens: Sonnet 5 $2/$10, Haiku 4.5 $1/$5,
  Opus 5 $5/$25, others listed in `costs.py`); unknown models use a conservative $5/$25.
* The default per-run budget is $3.00 (`limits.max_llm_cost_per_run_usd`).

## Collection and sources

* **Generic news source: GDELT DOC 2.0 API** (open, no key). Chosen because Google News RSS is disallowed
  by robots.txt and the Bing Search APIs were retired; GDELT covers any topic, country and language.
  Defaults: one combined query per run, up to 10 terms, `timespan` = `lookback_days`, at most 40 articles
  fetched per run, 6 s spacing and a 20 s retry floor for that host. `search_country` defaults to the
  region's GDELT country name (for example India -> `india`, United States -> `unitedstates`; regions such
  as "Europe" or "Global" search worldwide) and `search_language` to the first language hint.
* **LLM model order (tested 2026-09-25 on the project's free keys with a three-company extraction that
  has one right answer):** Gemini gemini-3.8-flash (answered; overloaded with HTTP 503 at other times),
  gemini-3.7-flash, 3.5-flash and 2.5-flash (3/3 correct each); gemini-3.6-flash returned no items and
  is excluded; Pro models were unavailable or had no free quota. NVIDIA nemotron-3-ultra-550b (3/3, ~10 s,
  intermittent HTTP 500) then nemotron-3-super-120b (3/3, ~1.5 s); DeepSeek V4.1 Flash and Kimi K3 took
  over 150 s and Kimi K2.6 was not served. Groq gpt-oss-120b (3/3, 1.4 s) and gpt-oss-20b (3/3, 1.0 s);
  qwen3.8-27b (preview) reversed a sign and the Llama models were not offered to the key. Nemotron models
  are called with reasoning off (`enable_thinking: false`), which fixed failing forced function calls.
* **Fallback cool-downs:** a spent daily quota skips that model for an hour; rate limiting or an outage
  that outlasts the retries skips it for 10 minutes; a wrong key or unknown model for an hour. Server
  errors are retried twice, rate limits four times (honouring `Retry-After`/`retryDelay`).
* **Workspace library:** 44 topics in 10 categories; example players are well-known companies per region
  (market data, not rankings), checked by the builder like any other input.
* **Workspace builder limits:** up to 12 entities, 6 aliases each, 8 aspects, 6 news search phrases,
  12 feeds, 12 official pages (2 per entity), 8 subreddits; feeds must have an item from the last 180
  days; pages need at least 200 characters of readable text; the builder's LLM calls have a $0.50 budget
  per draft (free tiers are recorded at $0). Descriptions must be 8-3000 characters.
* **Builder defaults for new workspaces:** `filter_feeds_by_relevance: true`, delivery channels
  `[telegram, email]` (still dry-run until credentials are set), 7-day lookback, 150 Reddit posts, 10
  YouTube videos with 50 comments each, the standard scoring weights.
* **Workspace storage:** files in `workspaces/` plus copies in `workspace_configs`; the file wins only if
  it changed after the stored copy was saved. Archived files move to `workspaces/_archived/`.
* **Dashboard runs** use `fieldnote run -w <id> --dry-run` in a background process (delivery stays in the
  outbox); a second run of the same workspace is refused while one is running, and a `running` row older
  than 3 hours is treated as crashed.
* **Private-address guard** on by default (`FIELDNOTE_ALLOW_PRIVATE_URLS=1` to disable). Hosts that do not
  resolve are not treated as internal (the request fails on its own).

* **URL verification (2026-09-24).** Candidate feeds and pages were checked with FieldNote's own client
  (robots.txt + fetch + feed parse). Kept: every URL now in the workspace files (all PASS in
  `fieldnote doctor`). **Dropped:**
  * `autocarindia.com/RSS/rss.ashx?type=all_bikes` (HTTP 410), `financialexpress.com/auto/feed/` (410),
    `91wheels.com/feed` (404), `91mobiles.com/hub/feed/` and `/feed` (404), `bikedekho.com/rss` (404),
    `zigwheels.com/rss` (404), `digit.in/rss-feed/` (404), `autocarindia.com/rss`, `carandbike.com/rss`,
    `autocarpro.in/rss` (HTML, not a feed);
  * `gadgets360.com/rss/news` and `mi.com/in/` (robots.txt returns 403, treated as disallow-all);
  * `motorola.com/in/en/` and `/smartphones` (HTTP 500), `po.co/in/` (redirects to a 404 page),
    `poco.in` (near-empty shell), `infinixmobility.com/in` (404; the global site is used instead),
    `honda2wheelersindia.com/activa-e` (404), and several product sub-pages that returned 404/503
    (`chetak.com/price`, `vidaworld.com/vida-v2`, `atherenergy.com/pricing`, TVS iQube price page).
  * Competitors whose pages could not be verified (Honda, Xiaomi, Motorola, Poco) remain as market data
    with `pages: []`; they are still tracked in news and customer voice.
* **Google News RSS** queries are kept in the workspace files but are skipped at runtime because
  news.google.com's robots.txt disallows `/rss` for crawlers (verified 2026-09-24).
* **Subreddits** (`indianbikes`, `CarsIndia`, `bangalore`, `electricvehicles`; `IndianGaming`, `india`,
  `Android`, `PickAnAndroidForMe`) are long-standing public communities but could not be verified via
  the API during the build because no Reddit credentials were available; `fieldnote run` reports any
  that fail.
* **Metrics connectors** point at `data/registrations.csv` and `data/smartphone_shipments.csv`, which you
  supply (see `docs/sources-and-compliance.md`). Until they exist, metrics are skipped with a warning.
* **Default lookback** is 7 days for a workspace's first run; afterwards each run collects since the
  previous run's as-of time.
* **Full-text fetching** is on for RSS items whose article URL is allowed by robots.txt, capped at
  `max_items_per_feed` (25) and `max_documents_per_source` (60).

## Processing

* **Ambiguous aliases.** Aliases of three characters or fewer are matched case-sensitively; aliases that
  are common English words (a fixed generic list, e.g. "Simple", "River") are confirmed by the fast
  model before counting as a mention.
* **Change detection thresholds.** A diff block counts if it changes a currency amount or any figure, or
  if at least 20 characters and 1% of the page changed. Timestamps, "N people viewing"-style counters,
  "updated ... ago", cookie and copyright lines are masked.
* **Significance** is 0.6 + |price change %| / 25 for prices (capped at 1), 0.8 for launches, 0.6 for
  policy, 0.5 for dealer/feature, 0.25 for other, scaled by diff size for non-price changes.
* **Themes** need at least 8 tagged posts in the last 14 days. Windows are the last 7 days vs the 7 days
  before. "Emerging" means at least 3 posts, at most 20% of current-window volume, and a share of voice
  at least double the previous window's (or new).
* **Claim vs reported** uses first-hand reports from the last 90 days attributed to exactly one brand;
  values outside `plausible_range` (or 0.1x-2.5x the claimed value) are discarded. Low confidence means
  `n < min_n` (10 for EV range, 8 for phone battery life).
* **Critic thresholds.** Excerpt match needs RapidFuzz partial ratio >= 85 (or an exact substring);
  time-sensitive claims whose newest source is older than 45 days are rejected; arithmetic tolerance is
  max(0.15, 0.5%) of the recomputed value; numbers may be a rounding of a source number (half a unit of
  the last stated digit).

## Findings and scoring

* Default weights: market size 0.30, competitor gap 0.25, evidence strength 0.30, effort 0.15.
* Evidence strength = 0.35 x min(sources/5, 1) + 0.20 x min(source types/3, 1) + 0.25 x mean recency
  (half-life 14 days) + 0.20 x (any primary source).
* A finding needs 2 verified evidence claims to be `active`; with 1 it is `demoted` ("thin evidence");
  with 0 it is dropped. Findings unseen for 21 days become `stale`.
* Findings merge across runs when category and entities match and titles are similar
  (token-sort ratio >= 92, or >= 80 with >= 20% source overlap).

## Outputs and delivery

* The daily brief's prose (what happened, why, action, how) is capped at `brief_max_words` (350) across
  the top findings; trimming drops whole trailing sentences first.
* Telegram messages use MarkdownV2 with full escaping and are split on line boundaries at
  `telegram_max_chars` (4000).
* **Delivery requires both** the channel listed in the workspace `delivery.channels` **and** credentials
  in the environment; otherwise (or with `--dry-run`) output goes to `out/<workspace>/outbox/`.
* With `output.anonymize_entities: true`, labels are "Market Leader" (largest share in the first metrics
  connector) then "Challenger A", "Challenger B"...; without metrics, config order is used and all are
  "Challenger" labels. Evidence moves to an appendix that keeps real names and links.

## Coordination

* Relative due dates (resolved in code against the note date, weeks start Monday):
  today/EOD = note date; tomorrow = +1; "<weekday>"/"by <weekday>" = next occurrence after the note date;
  "next <weekday>" = that weekday in the following calendar week; "this week"/"end of week" = Friday of
  the note's week; "next week" = Friday of the following week; "end of month"/"next month" = last day of
  that month; "end of quarter" = last day of the quarter; "in N days/weeks"; explicit dates without a
  year take the next occurrence (a date more than 30 days in the past rolls to next year).
* Owners must be named in the note; pronouns, "we", "team", "TBD" become `Unassigned` with an
  `ambiguous_owner` flag; no name at all is `missing_owner`; a name not in the attendee list is flagged.
* Open items with a description similarity >= 85 (and the same or an unassigned owner) are offered as
  merges; merging keeps history, the earlier due date and fills a missing owner.
* Reminders cover overdue items and items due within `reminders.due_within_days` (2), at most once per
  item per day; items are never auto-completed.

## Fixtures and evals

* Fixture datasets are synthetic, use fictional brands and `.example` URLs, and contain one prompt-
  injection post and one hidden-text injection per workspace on purpose.
* Eval thresholds (`evals/datasets/thresholds.json`): citation validity >= 95%, adversarial catch rate
  >= 90% (mock path), number/entity consistency >= 99%, extraction precision and recall >= 80%.
* The adversarial eval corrupts up to 40 verified claims per run in four ways (change a number, swap an
  entity, fabricate a fact, flip a direction word where one exists).
