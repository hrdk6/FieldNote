# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Python package (`src/fieldnote`) with SQLAlchemy over SQLite or Postgres. The dashboard is being rebuilt as a
React + Vite + TypeScript single-page app in `web/`, served by a small Starlette JSON API (Starlette and
uvicorn are already dependencies) over the existing `db.repo` layer. The new web UI replaces the Streamlit
dashboard once it covers every page.

## Users

A strategy lead or founder who opens FieldNote each morning to see what changed in their market, which
findings matter most, and what to do next. They scan quickly, drill into evidence only when a finding
matters, and act (assign actions, ask a question, read the brief). Secondary: whoever sets up workspaces
and inspects runs, usually the same person.

## Product Purpose

FieldNote is a configurable AI chief-of-staff for market and competitor intelligence. A workspace is
described in plain language; FieldNote collects public information (news, competitor pages, Reddit,
YouTube, supplied metrics), detects what changed, runs a researcher / critic / analyst / writer pipeline,
ranks findings deterministically, answers questions with citations, tracks action items from meeting notes,
and delivers a daily brief and weekly memo. Success: the lead trusts the morning read enough to act on it.

## Positioning

Every statement is a claim with a source and a verbatim excerpt, checked by a critic before anyone sees it.
Arithmetic lives in code, ranking is a deterministic weighted sum re-rankable without the LLM, and when
evidence is missing FieldNote says so.

## Operating Context

Daily morning read on a laptop; occasional deeper sessions for workspace setup, run inspection and evals.
Pages today: Overview, Market & competitors (change feed with diffs), Metrics, Customer voice, Opportunity
Board (weight sliders re-rank live), Ask FieldNote, Briefs, Actions, Run Inspector, Evals, Workspaces
(create from plain language or a template library, verify sources, run, archive). Optional password gate
and read-only mode for hosted deployments.

## Capabilities and Constraints

- Claim types: fact, estimate, analysis. Findings can be active, demoted ("thin evidence"), stale, disputed.
- The dashboard is a live application: it shows live data only and keeps it current on its own (source checks
  every 30 min, full analysis every 6 h, daily delivery; `fieldnote live` or the in-process scheduler). Pages
  update in place via Server-Sent Events. Fixture/demo data (fictional brands, `.example` links) appears only
  with `fieldnote dashboard --demo` and must always be labelled as such.
- Entity colours are assigned by config order and stay stable across every chart and filter.
- `FIELDNOTE_DASHBOARD_PASSWORD` and `FIELDNOTE_DASHBOARD_READONLY` must keep working.
- Light and dark themes must both work.

## Brand Commitments

Name "FieldNote". Voice is plain, precise and sourced; no hype.

## Evidence on Hand

Fixture workspaces (EV two-wheelers India, quick commerce India, budget smartphones India) in `data/demo.db`
and `out/`. No real customers, testimonials or benchmarks exist; do not invent them.

## Product Principles

1. Trust is the product: source, excerpt and verification status stay visible next to every claim.
2. Scan first, drill on demand: the morning read takes minutes; evidence is one step away, never in the way.
3. Deterministic and inspectable: show the scores, weights, runs and checks behind every ranking.
4. Honest about gaps: demo data, thin evidence, disputes and missing data are labelled, never hidden.

## Accessibility & Inclusion

No product-specific standard confirmed; follow WCAG AA contrast and keyboard access as the floor.
Motion must never slow down the daily scan.
