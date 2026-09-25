# Architecture

FieldNote is a single Python package (`src/fieldnote`) with a Typer CLI, a Streamlit dashboard and a
scheduled pipeline. Everything sector-specific lives in `workspaces/<slug>.yaml`; the code only knows
the *shape* of a workspace.

## Pipeline

```mermaid
flowchart LR
    subgraph Collect["1 · Collect (isolated, robots.txt + rate limits)"]
        N[rss_news<br/>RSS / Google News RSS] --> S
        P[pages<br/>competitor pages] --> S
        R[reddit<br/>official API] --> S
        Y[youtube<br/>Data API v3] --> S
        C[connectors<br/>csv_file / http_json] --> M
        S[(documents<br/>page_snapshots)]
    end
    subgraph Process["2 · Process (code first, fast model second)"]
        S --> CL[clean + dedupe]
        CL --> CD[change detection<br/>difflib + rules]
        CL --> ET[entity tagging]
        CL --> VT[voice tagging]
        VT --> TH[themes<br/>TF-IDF + KMeans]
        VT --> CVR[claim vs reported<br/>median / IQR / n]
        M[(metric_points)] --> MT[metrics<br/>MoM, share, movers]
    end
    subgraph Agents["3 · Agents (explicit state machine)"]
        CTX[build context] --> RS[Researcher<br/>bounded tool loop]
        RS --> CF[Critic<br/>stage 1 code + stage 2 LLM]
        CF --> AN[Analyst]
        AN --> CA[Critic<br/>overreach + counter-evidence]
        CA --> SC[Deterministic scoring]
        SC --> WR[Writer<br/>prose only + post-check]
        WR --> PS[(claims, findings)]
    end
    subgraph Out["4 · Outputs"]
        PS --> DB[daily brief<br/>MD / HTML / Telegram]
        PS --> WM[weekly memo PDF]
        DB --> DL[delivery<br/>Telegram / email / dry-run]
        WM --> DL
        PS --> UI[Streamlit dashboard]
        PS --> ASK[Ask FieldNote]
    end
    CD --> CTX
    TH --> CTX
    CVR --> CTX
    MT --> CTX
```

## Trust model: claims and the critic

```mermaid
sequenceDiagram
    participant R as Researcher (LLM)
    participant T as Read-only tools
    participant C as Critic
    participant A as Analyst (LLM)
    participant W as Writer (LLM)
    R->>T: search_documents / get_document / get_changes / get_metric_summary ...
    T-->>R: data wrapped in <untrusted_content>
    R->>C: fact + estimate claims (source ids, verbatim excerpts, derivations)
    C->>C: stage 1: source exists, excerpt found, numbers/entities/dates, arithmetic re-computed
    C->>C: stage 2: LLM entailment (supported / partially / unsupported / contradicted)
    C-->>A: verified claims only
    A->>C: findings + analysis claims (with dependencies)
    C->>C: overreach check, counter-evidence search, minimum evidence
    C-->>W: verified claims + scored findings (never raw documents)
    W-->>W: post-check: every number/name must exist in a verified claim, else regenerate, else template
```

## Modules

| Layer | Modules | Notes |
|---|---|---|
| Config | `config.py` | Pydantic v2 schema for workspaces, env `Settings`, fixture overlay, path sanitising, region profiles |
| Workspaces | `workspaces.py`, `builder/*` | Registry of file + database copies (archive, restore, import); plain-language builder that drafts, verifies and assembles workspaces |
| Storage | `db/engine.py`, `db/models.py`, `db/repo.py` | SQLAlchemy 2.0; SQLite default, Postgres via `DATABASE_URL`; versioned `create_all` + additive column migration |
| LLM | `llm/base.py`, `llm/anthropic_client.py`, `llm/mock_client.py`, `llm/mock_agents.py`, `llm/cache.py`, `llm/schemas.py` | Provider-neutral interface; forced tool use for structured output; cache + cost tracker on every call |
| Collection | `collectors/*`, `collectors/connectors/*`, `collectors/fixtures.py` | One interface; polite HTTP client (robots, pacing, private-address guard); GDELT news search; connector registry; offline replay |
| Processing | `processing/*`, `heuristics.py` | Cleaning, dedupe, change detection, tagging, themes, claim vs reported, metrics |
| Agents | `agents/*` | Tools, researcher, critic, analyst, writer, orchestrator, tracer |
| Scoring | `scoring/opportunities.py` | Pure functions; re-rankable without LLM calls |
| Ask | `ask/router.py`, `ask/answer.py` | Local-only retrieval, critic-gated answers, session memory |
| Coordination | `coordination/*` | Action-item extraction, tracker, reminders, Sheets export |
| Reports | `reports/*` | Jinja2 templates, reportlab PDF, matplotlib charts |
| Delivery | `delivery/*` | Telegram, SMTP, dry-run outbox, routing |
| Evals | `evals/*` | Citation validity, adversarial critic, number consistency, extraction P/R, ranking, cost |
| Entry points | `cli.py`, `pipeline.py`, `doctor.py`, `exporter.py`, `dashboard/*` | |

## Data model

All tables carry `workspace` and timestamps: `runs`, `documents`, `page_snapshots`, `change_events`,
`metric_points`, `voice_tags`, `themes`, `claim_vs_reported`, `claims`, `findings`, `briefs`,
`meeting_notes`, `action_items`, `llm_cache`, `agent_traces`, plus `http_cache` (conditional requests),
`eval_results`, `workspace_configs` (workspace YAML shared by hosted dashboards and schedulers) and
`schema_version`.

## Workspace builder

```mermaid
flowchart LR
    U[Plain-language description] --> D[Draft<br/>LLM or rule-based]
    D --> V[Verify every source<br/>feeds · sites · pages · news search · subreddits]
    V --> A[Assemble<br/>limits · aliases · aspects · region]
    A --> S[Strict schema validation<br/>+ YAML round-trip]
    S --> R[Review in dashboard or CLI]
    R --> W[(workspaces/*.yaml + workspace_configs)]
```

The description is wrapped as untrusted input in the prompt. The model can only propose; code verifies
and assembles, so an unverifiable URL, a duplicated alias or an invalid aspect never reaches a saved
workspace.

## Idempotency and history

* A run is keyed by (workspace, local date, mode, kind). Re-running the same day resets that run's
  derived rows (claims, themes, changes, traces, snapshots) and rebuilds them; documents are upserted
  by URL / content hash, so nothing is duplicated.
* Findings persist across runs and are merged by fuzzy title plus entity and evidence overlap;
  `first_seen`/`last_seen` are maintained and unseen findings become `stale`.
* Offline mode replays fixture *rounds* (a baseline week, then the current week) so change detection
  and week-over-week themes have history on the very first run.

## Degradation

Every collector, processing step and agent stage is isolated. Failures are logged, recorded in
`runs.stats`, and the run continues: missing keys skip collectors, LLM failures fall back to
deterministic heuristics or templates, an exhausted budget stops LLM calls but keeps verified output,
and delivery falls back to the dry-run outbox.
