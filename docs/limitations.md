# Limitations

FieldNote is a decision-support tool. Read its output as a well-sourced starting point, not a verdict.

## Data coverage

* **Public data only.** FieldNote uses public web pages, RSS feeds and the official Reddit and YouTube
  APIs. It does not log in anywhere, bypass paywalls or CAPTCHAs, or collect from sources whose terms
  forbid automated access. Anything behind those walls is invisible to it.
* **robots.txt shapes coverage.** Disallowed pages and feeds are skipped. At the time of writing,
  `news.google.com` disallows crawlers on `/rss`, so Google News queries in workspace files are skipped;
  news comes from the configured RSS feeds instead.
* **GDELT is best-effort.** The open news index rate-limits aggressively (a burst of requests can lead to
  minutes of HTTP 429) and indexes some publishers late. Runs continue with the other sources and mark
  the news search as degraded; verified RSS feeds are the dependable backbone of a workspace.
* **Free LLM tiers vary by the hour.** Newer models get overloaded, NVIDIA returns intermittent server
  errors, daily quotas run out, and Groq's free tier caps tokens per minute (8K for gpt-oss), so a very
  large prompt can only be served by Gemini or NVIDIA. The model chain and cool-downs absorb this, but
  output quality can shift between runs depending on which model answered.
* **Builder drafts need a human look.** The model can pick broad publications, generic subreddits or an
  odd perspective, and the rule-based drafter (no LLM key) only knows the names in your description. The
  review screen and the dropped-source list make this quick to fix, but read the draft before relying on
  it. Relevance filtering of feed items looks at titles and summaries only.
* **JavaScript-heavy pages.** Pages that render content client-side need `render: true` and the optional
  Playwright extra; without it FieldNote fetches the static HTML, which may miss prices or text.
* **Customer voice is sampled and non-representative.** Reddit threads and YouTube comments skew toward
  enthusiasts, recent buyers and people with problems. Theme sizes and sentiment describe what was
  collected, not the whole customer base. Hinglish and code-mixed posts are supported but tagged less
  reliably than English.
* **Market and registration metrics depend on what you supply.** Metrics exist only if a connector is
  configured and fed with data you obtained through sanctioned means. FieldNote does not scrape
  government or industry dashboards.

## Model and method limits

* **LLM extraction errors.** Entity resolution, voice tagging, reported-value extraction and action-item
  extraction can be wrong. The critic reduces fabricated or mis-numbered claims, and every claim links
  to its source so a person can check it, but no automated check is perfect.
* **The critic is not omniscient.** Stage 1 catches changed numbers, swapped entities, fabricated
  excerpts, stale sources and wrong arithmetic very reliably. Semantic errors that keep the same numbers
  and names (for example a subtle change of meaning) depend on the stage-2 model. The eval report
  measures this with programmatic corruptions; the offline (mock) critic's catch rate is reported
  separately from the live-model rate.
* **Estimates are approximations.** Medians over a handful of self-reported values, shares over the
  tracked entities only, and month-over-month changes on user-supplied data are labelled "Estimate".
  Comparisons with fewer reports than `min_n` are flagged low-confidence and never stated as fact.
* **Themes are statistical.** TF-IDF + KMeans clusters depend on vocabulary and corpus size; with small
  corpora (fewer than 8 tagged posts) themes are skipped, and labels are short summaries, not findings.
* **Scores are structured judgement, not truth.** Market size, competitor gap and effort ratings come
  from the analyst model (with written rationales); only evidence strength is computed. Weights are a
  policy choice you can change.
* **Relative dates.** "Next Friday"-style phrases are resolved by documented rules (see
  `docs/assumptions.md`); teams that use different conventions should write explicit dates.

## Operational limits

* **Cost and latency** depend on corpus size and the model. The per-run budget stops LLM calls at the
  configured limit and keeps whatever is verified; very large corpora may therefore produce fewer
  findings rather than higher bills.
* **Single-writer database.** SQLite suits one scheduled pipeline plus a dashboard. Use Postgres via
  `DATABASE_URL` for concurrent writers or hosted deployments.
* **Offline demo quality.** The MockClient is deterministic and rule-based. Its briefs are faithful to
  the fixtures but less fluent and less insightful than a live model's.

## Not a substitute for human judgement

FieldNote does not know your strategy, your costs, or what your team already knows. Treat findings as
prompts for discussion, verify anything consequential against the linked sources, and decide.
