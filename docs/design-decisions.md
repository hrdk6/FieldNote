# Design decisions

## 1. Claims are the unit of trust

Every statement that can reach an output is a *claim* of type `fact`, `estimate` or `analysis`.
Facts cite at least one stored document and a verbatim excerpt; estimates state their method and a
machine-checkable derivation; analysis lists the claims it depends on. Findings reference claims by
id, and briefs render only verified claims. This makes "where did this come from?" answerable for
every sentence and lets evals measure citation validity mechanically.

## 2. A critic gates everything, deterministic checks first

LLM-only verification is expensive and can be talked into things. The critic runs cheap, exact checks
first (source exists, excerpt found, every number present or re-computable, entity and proper-name
checks, staleness, injection patterns, low-confidence labelling) and only then asks the model for an
entailment judgement. Rejected claims are dropped; claims without a stage-2 verdict (budget exhausted,
API down) are *withheld*, never shown. Facts need `supported`; estimates may be `partially_supported`
because their numbers are re-derived in code.

## 3. Arithmetic lives in code

Month-over-month changes, shares, medians, IQRs, theme counts and sentiment means are computed by
FieldNote. When a claim uses them, its derivation points at the computed data (`op: pct_change`,
`op: reference`), and the critic recomputes the value. The LLM never does sums.

## 4. Deterministic scoring, re-rankable without the LLM

The analyst supplies 1-5 ratings with rationales for market size, competitor gap and effort;
evidence strength is computed from verified sources (count, type diversity, recency half-life,
primary-source presence). The total is a weighted sum with fixed tie-breaks (evidence count, recency,
title, id). The same inputs always produce the same order; dashboard sliders re-rank from stored
normalised criteria with zero LLM calls. Both properties are tested and evaluated.

## 5. Read-only tools, delimited data, no model-triggered actions

Agents can only call parameterised read tools (`search_documents`, `get_document`, `get_changes`,
`get_metric_series`, `get_metric_summary`, `get_themes`, `get_claim_vs_reported`, `get_findings`).
There is no SQL tool, no write tool and no send tool. Collected text is wrapped in
`<untrusted_content>` delimiters (closing tags inside content are neutralised), hidden HTML is stripped,
and system prompts tell every agent that such content is data. Delivery is triggered only by the
CLI/scheduler and only to channels configured in the workspace *and* the environment. The writer never
sees raw documents, only verified claim text.

## 6. Provider abstraction with a first-class mock

`LLMClient` has two implementations. `AnthropicClient` uses the official SDK with forced tool use for
structured output (Pydantic schema -> tool input schema), retries validation failures up to twice with
the error fed back, and falls back to `tool_choice: auto` plus an instruction on models that reject
forced tool choice. `MockClient` is deterministic and rule-based; it drives the exact same code paths
(tool loop, claim schema, critic, scoring, writer post-checks), so the whole system runs, and is
tested, without network or keys.

`OpenAICompatClient` adds the free Google Gemini and NVIDIA NIM tiers through their OpenAI-compatible
chat-completions endpoints. The agents keep speaking Anthropic-style content blocks; the client
translates them to OpenAI messages and back, echoes Gemini thought signatures only to Gemini, strips
JSON-schema keywords those backends reject (Pydantic still validates the full schema), accepts a JSON
object in plain text when a model ignores the forced function call, throttles to a per-provider RPM and
backs off on 429/5xx. `FallbackClient` chains providers so a quota-exhausted free tier hands the same call
to the next one. Every call goes through a response cache (key = hash of model +
prompt + schema) and a per-run cost tracker with a hard budget.

## 7. Explicit state machine, not an agent framework

The orchestrator is a small `Stage` enum with a transition table. Each stage has typed inputs/outputs
on `PipelineState`, is traced to `agent_traces`, and can fail or run out of budget without aborting the
run. This keeps behaviour inspectable (Run Inspector) and avoids framework lock-in.

## 8. Config-driven sectors, fictional fixtures

No sector or company knowledge lives in code: competitors, aliases, aspects and their keywords,
metrics vocabulary, connectors and scoring weights are YAML. Offline fixtures use fictional brands and
`.example` URLs through a manifest overlay, so demo output can never be mistaken for real market
claims, while the shipped workspace files list the real market for live use.

## 9. Change detection favours precision

Page text is normalised (boilerplate removed, timestamps/counters/cookie banners masked,
configurable ignore patterns), diffed by line with `difflib`, and small diffs are ignored unless a
price or figure changed. Rules classify first (price delta, launch words, policy, dealer, feature);
the fast model only sees what the rules could not place. Summaries are generated deterministically so
they can be cited verbatim. Every change also stores a *change record* document holding before/after
text, which is what claims cite.

## 10. SQLite by default, Postgres-ready

SQLite keeps the demo zero-setup; `DATABASE_URL` switches to Postgres (the `postgres` extra installs
psycopg). Schema management is a versioned `create_all` plus additive column migration, which is enough
for a single-service tool; Alembic can be introduced later without changing the models.

## 11. Honest outputs over complete outputs

Low-sample comparisons are flagged and never stated as fact; estimates are labelled; disagreements
between sources are recorded rather than resolved; Ask says what evidence is missing; findings with
thin evidence are shown as such; stale findings are marked. Word limits are enforced by dropping whole
trailing sentences, never by rewriting.

## 12. The model proposes, code verifies (workspace builder)

Making FieldNote general means letting people describe any topic, but a model asked for sources will
sometimes invent URLs, merge aliases or return aspects that are not snake_case. So the builder splits the
work: the LLM drafts (entities, aliases, aspects, search phrases, candidate sources); code verifies every
URL with the same polite client the collectors use, removes what fails, resolves alias collisions,
normalises aspects and region words, and validates the result with the strict workspace schema before a
human reviews it. Without a key the same flow runs on a rule-based drafter that only uses names from the
description. News for arbitrary topics comes from GDELT's open API (Google News RSS is disallowed by
robots.txt), with a relevance filter because GDELT matches article bodies loosely.

## 13. Workspaces live in files and in the database

Files stay the primary format (diffable, reviewable, easy to commit). A hosted dashboard, though, often
runs on a read-only or ephemeral filesystem and on a different machine from the scheduler, so created
and edited workspaces are also stored in `workspace_configs` in the shared database. Each stored copy
records the SHA-256 of the file version it superseded; the file wins only if it changed after that.
Timestamps were rejected because a git checkout resets modification times.

## 14. Best model first, with a circuit breaker (free tiers)

Free tiers change hour by hour: the newest model is overloaded one minute and fine the next, daily quotas
run out mid-run, and one provider's endpoint returns 500s. Each provider therefore contributes an ordered
list of models (tested, best first) and the chain is Gemini -> NVIDIA -> Groq. A failure that means "this
model cannot serve now" raises `LLMUnavailableError` with a cool-down; the chain skips that model for
the cool-down so only one call pays the retry cost, and if every model is cooling down it tries them all
again rather than failing without a request. A content failure (the model answered but the output was
invalid) is not treated as unavailability.
