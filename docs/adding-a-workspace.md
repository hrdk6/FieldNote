# Adding a workspace

A workspace is one YAML file. No code changes are needed.

## Quickest: pick a topic from the library

Dashboard: **Workspaces → New workspace**, choose a region, filter by category or search, press *Use
this*. Terminal:

```bash
fieldnote workspace templates payments --region "United States"
fieldnote workspace new --template digital_payments --region "United States"
```

The library has 44 topics in 10 categories, each with example players for India, the United States, the
United Kingdom and/or globally. Any other region works too: the model chooses the players. The chosen
topic goes through the same builder as a typed description (below), so every source is verified.

## The quick way: describe it (about a minute)

Dashboard: **Workspaces → New workspace**, type what you want to track, press *Draft workspace*, review,
press *Create workspace*. Terminal:

```bash
fieldnote workspace new "Home appliances in the UK: Bosch, Hotpoint, Beko, Samsung"
```

Options: `--region`, `--perspective`, `--focal-company`, `--id`, `--language`, `--no-verify` (no network;
no URLs are added), `--print` (show the YAML without saving), `--yes` (no confirmation prompt).

The builder drafts entities and aliases, aspects, news search phrases and candidate sources, then checks
every source before anything is saved:

| Candidate | Kept when |
|---|---|
| RSS/Atom feed | robots.txt allows it, it parses, it has items, and the newest item is under 180 days old |
| Publication or company site | its advertised feed (`<link rel="alternate">`) or `/feed` passes the feed check |
| Official page (pricing, product, newsroom, dealers) | robots.txt allows it and it returns at least 200 characters of readable text |
| News search phrases | GDELT accepts the query; if the region's sources have no recent hits, the search widens to worldwide |
| Subreddit | the Reddit API confirms it exists and is not NSFW (kept as "unverified" without Reddit keys) |

Anything dropped is listed, with the reason, at the end of the YAML. URLs you paste into the
description are verified the same way. Region words are removed from search phrases ("Blinkit India" ->
"Blinkit") because results are already filtered to the region's sources, and new workspaces keep only
feed items that mention an entity, alias, keyword or search phrase (`filter_feeds_by_relevance: true`).

The workspace is saved to `workspaces/<id>.yaml` and to the database. Review it like any other file; the
sections below explain every field.

## By hand

### 1. Create the file (30 seconds)

```bash
fieldnote init -w home_appliances_uk
```

This copies `workspaces/_template.yaml` to `workspaces/home_appliances_uk.yaml` with the slug filled in.
The slug must be lowercase letters, digits and underscores, and must match the file name.

### 2. Fill in the basics (2 minutes)

```yaml
workspace: home_appliances_uk
display_name: "Home appliances, UK"
sector: "home appliances"
region: "United Kingdom"          # also sets the default news locale
timezone: "Europe/London"
focal_company: null               # or your company's name
perspective: "mid-market appliance brand"
competitors:
  - name: "Brand A"
    aliases: ["Brand A Home"]
    pages:
      - {url: "https://www.brand-a.example/pricing", kind: pricing}
keywords: ["washing machine", "energy rating"]
aspects: ["price", "reliability", "noise", "delivery", "service"]
aspect_keywords:
  noise: ["noise", "noisy", "loud", "quiet"]
```

Tips:

* Add aliases people actually use (model names, short brand names). Very short aliases are matched
  case-sensitively.
* `aspects` drive customer-voice tagging; `aspect_keywords` help the offline tagger and Ask.
* Leave `connectors` empty until you have a sanctioned data file.

### 3. Add sources (1 minute)

```yaml
sources:
  news:
    search_queries: ["washing machine", "Hotpoint"]   # GDELT topic search, no key needed
    rss_feeds: ["https://www.some-trade-publication.example/feed"]
  reddit: {subreddits: ["SomeCommunity"], search_terms: ["washing machine"], max_posts_per_run: 200}
  youtube: {search_terms: ["washing machine review"], max_videos: 10, max_comments_per_video: 50}
```

Optional: a claimed-vs-reported metric, e.g. energy use:

```yaml
claim_vs_reported:
  - {metric: "energy_use", unit: "kWh", keywords: ["energy", "kwh per cycle"], min_n: 10, plausible_range: [0.1, 5]}
```

### 4. Validate (1 minute)

```bash
fieldnote doctor -w home_appliances_uk
```

`doctor` validates the YAML (with field-level error messages), checks env vars and credentials, and
fetches every feed and page to confirm it resolves and that robots.txt allows FieldNote. Remove any URL
it marks FAIL; WARN on robots means FieldNote will skip it.

### 5. Run (30 seconds)

```bash
fieldnote run -w home_appliances_uk --dry-run
fieldnote dashboard
```

The first run establishes page snapshots, so "What changed" fills in from the second run onward.
Delivery stays in the dry-run outbox until you list channels under `delivery.channels` and set the
Telegram/SMTP environment variables.

## Where workspaces live

`fieldnote workspace list` shows every workspace and where it is stored. A workspace can be a file in
`workspaces/`, a copy in the database (created or edited in a hosted dashboard), or both. When both
exist, the database copy records which file version it replaced: if the file has changed since (you
edited it or pulled a newer commit), the file wins; otherwise the database copy wins. Archiving
(`fieldnote workspace archive <id>`) moves the file to `workspaces/_archived/`, hides the workspace from
lists and scheduled runs, and keeps its data; `restore` brings it back.

## Optional: offline fixtures for a new workspace

To demo a workspace without network access, add `fixtures/<slug>/manifest.json` (rounds plus an
overlay of fictional competitors) and `news.json`, `pages.json`, `reddit.json`, `youtube.json`,
`metrics.csv`. `scripts/build_fixtures.py` shows the format for the two shipped workspaces.
