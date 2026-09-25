import { AnimatePresence, LayoutGroup, motion } from "motion/react";
import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Archive, ArchiveRestore, Check, Download, Minus, Play, Plus, RefreshCw, Save, Upload, WandSparkles } from "lucide-react";
import { api, invalidate, useApi, type RunInfo } from "../api";
import { Disclosure, EASE, Empty, ErrorNote, Exhibit, Mark, Note, PageHead, PageSkeleton, Reveal, Sk, Spinner, useToast } from "../components/ui";
import { useShell } from "../shell";
import { ago, elapsed, until, useLive, useNow } from "../live";
import { fmtDateTime } from "../util";

const TABS = [
  { id: "new", label: "New workspace" },
  { id: "this", label: "This workspace" },
  { id: "all", label: "All workspaces" },
] as const;
type Tab = (typeof TABS)[number]["id"];

const EXAMPLES = [
  "Quick-commerce grocery delivery in India: Blinkit, Zepto, Swiggy Instamart, BigBasket",
  "UK challenger banks such as Monzo, Starling and Revolut",
];

// ---- library -------------------------------------------------------------------------------------

interface Library {
  categories: string[];
  regions: string[];
  total: number;
  drafter: string;
  is_mock: boolean;
  templates: { id: string; title: string; category: string; blurb: string; players: string[] }[];
}

function LibraryPicker({ onUse, busy }: { onUse: (id: string, region: string) => void; busy: boolean }) {
  const [q, setQ] = useState("");
  const [region, setRegion] = useState("India");
  const [other, setOther] = useState("");
  const [cat, setCat] = useState("All");
  const reg = region === "__other" ? other.trim() : region;
  const params = new URLSearchParams({ q, region: reg || "Global", ...(cat !== "All" ? { category: cat } : {}) });
  const { data } = useApi<Library>(`/library?${params}`);
  const groups = useMemo(() => {
    const m = new Map<string, Library["templates"]>();
    (data?.templates ?? []).forEach((t) => m.set(t.category, [...(m.get(t.category) ?? []), t]));
    return [...m.entries()];
  }, [data]);
  return (
    <>
      <div className="toolbar">
        <label className="field" style={{ flex: "2 1 260px" }}>
          <span>Search</span>
          <input className="input" type="search" value={q} placeholder="e.g. payments, airlines, Zomato, solar" onChange={(e) => setQ(e.target.value)} />
        </label>
        <label className="field" style={{ flex: "1 1 180px" }}>
          <span>Region</span>
          <select className="select" value={region} onChange={(e) => setRegion(e.target.value)}>
            {(data?.regions ?? ["India", "United States", "United Kingdom", "Global"]).map((r) => (
              <option key={r}>{r}</option>
            ))}
            <option value="__other">Other region…</option>
          </select>
        </label>
        {region === "__other" ? (
          <label className="field" style={{ flex: "1 1 180px" }}>
            <span>Country or region</span>
            <input className="input" value={other} placeholder="e.g. Germany, Brazil" onChange={(e) => setOther(e.target.value)} />
          </label>
        ) : null}
      </div>
      <div className="chips" style={{ marginTop: 14 }}>
        {["All", ...(data?.categories ?? [])].map((c) => (
          <button key={c} type="button" className="chip" aria-pressed={c === cat} onClick={() => setCat(c)}>
            {c}
          </button>
        ))}
      </div>
      {!data ? (
        <div style={{ marginTop: 20 }}>
          {[0, 1, 2, 3].map((i) => (
            <Sk key={i} h={40} style={{ marginTop: 10 }} />
          ))}
        </div>
      ) : data.templates.length ? (
        <>
          <p className="hint" style={{ marginTop: 14 }}>
            {data.templates.length} of {data.total} topics. Choosing one drafts a workspace for {reg || "your region"} with the {data.drafter}; every source is verified
            before you save.
          </p>
          {groups.map(([c, ts]) => (
            <div key={c}>
              <div className="lib-cat">{c}</div>
              <div className="lib">
                {ts.map((t) => (
                  <div className="lib-row" key={t.id}>
                    <div>
                      <b>{t.title}</b>
                      <div className="hint">{t.blurb}</div>
                    </div>
                    <div className="hint" style={{ marginTop: 0 }}>
                      {t.players.length
                        ? `Players: ${t.players.join(", ")}`
                        : data.is_mock
                          ? "No example players for this region; add an LLM key or name them yourself below."
                          : "Players: chosen by the model for this region."}
                    </div>
                    <button type="button" className="btn sm" disabled={!reg || busy} onClick={() => onUse(t.id, reg)}>
                      Use this
                    </button>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </>
      ) : (
        <p className="hint" style={{ marginTop: 16 }}>
          No template matches. Describe your own topic below.
        </p>
      )}
    </>
  );
}

// ---- draft + review ------------------------------------------------------------------------------

interface Draft {
  display_name: string;
  workspace: string;
  perspective: string;
  focal_company: string;
  region: string;
  timezone: string;
  language_hints: string[];
  competitors: { name: string; aliases: string[]; pages: { url: string; kind: string }[] }[];
  aspects: { aspect: string; words: string[] }[];
  search_queries: string[];
  rss_feeds: string[];
  subreddits: string[];
  reddit_terms: string[];
  youtube_terms: string[];
  checks: { kind: string; status: string; value: string; detail: string }[];
  warnings: string[];
  counts: { ok: number; dropped: number; unverified: number };
  drafted_by: string;
  yaml: string;
}

function Progress({ lines, state }: { lines: string[]; state: string }) {
  return (
    <ul className="steps" aria-live="polite">
      <AnimatePresence initial={false}>
        {lines.map((l, i) => {
          const current = i === lines.length - 1 && state === "running";
          return (
            <motion.li
              key={i}
              className={current ? "current" : undefined}
              initial={{ opacity: 0, x: -6 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ duration: 0.24, ease: EASE }}
            >
              {current ? <Spinner /> : <Check size={14} color="var(--verified)" strokeWidth={2.6} />}
              <span>{l}</span>
            </motion.li>
          );
        })}
      </AnimatePresence>
      {!lines.length ? (
        <li className="current">
          <Spinner />
          <span>Starting…</span>
        </li>
      ) : null}
    </ul>
  );
}

const csv = (s: string) =>
  s
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);

function Checklist({ label, items, selected, onChange, format }: { label: string; items: string[]; selected: string[]; onChange: (v: string[]) => void; format?: (s: string) => string }) {
  return (
    <div className="field wide">
      <span>
        {label} ({selected.length} of {items.length})
      </span>
      {items.length ? (
        <div className="check-list">
          {items.map((it) => (
            <label key={it} className="check">
              <input type="checkbox" checked={selected.includes(it)} onChange={(e) => onChange(e.target.checked ? [...selected, it] : selected.filter((x) => x !== it))} />
              {format ? format(it) : it}
            </label>
          ))}
        </div>
      ) : (
        <span className="hint">None proposed.</span>
      )}
    </div>
  );
}

function Review({ job, draft, onDiscard, onCreated }: { job: string; draft: Draft; onDiscard: () => void; onCreated: (name: string, msg: string) => void }) {
  const [display, setDisplay] = useState(draft.display_name);
  const [slug, setSlug] = useState(draft.workspace);
  const [persp, setPersp] = useState(draft.perspective);
  const [focal, setFocal] = useState(draft.focal_company);
  const [ents, setEnts] = useState(draft.competitors.map((c) => ({ name: c.name, aliases: c.aliases.join(", ") })));
  const [asps, setAsps] = useState(draft.aspects.map((a) => ({ aspect: a.aspect, words: a.words.join(", ") })));
  const [queries, setQueries] = useState(draft.search_queries.join("\n"));
  const [feeds, setFeeds] = useState(draft.rss_feeds);
  const pageLabels = useMemo(() => new Map(draft.competitors.flatMap((c) => c.pages.map((p) => [p.url, `${c.name} · ${p.kind} · ${p.url}`] as const))), [draft]);
  const [pages, setPages] = useState([...pageLabels.keys()]);
  const [subs, setSubs] = useState(draft.subreddits);
  const [rterms, setRterms] = useState(draft.reddit_terms.join(", "));
  const [yterms, setYterms] = useState(draft.youtube_terms.join(", "));
  const [runAfter, setRunAfter] = useState(true);
  const [check, setCheck] = useState<{ yaml: string; error: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const edits = {
    display_name: display,
    workspace: slug,
    perspective: persp,
    focal_company: focal,
    entities: ents.map((e) => ({ name: e.name, aliases: csv(e.aliases) })),
    aspects: asps.map((a) => ({ aspect: a.aspect, words: csv(a.words) })),
    search_queries: queries.split("\n"),
    rss_feeds: feeds,
    pages,
    subreddits: subs,
    reddit_terms: csv(rterms),
    youtube_terms: csv(yterms),
  };
  const key = JSON.stringify(edits);
  useEffect(() => {
    const t = window.setTimeout(() => {
      api<{ yaml: string; error: string }>(`/builder/jobs/${job}/review`, { method: "POST", json: JSON.parse(key) })
        .then(setCheck)
        .catch((e) => setCheck({ yaml: "", error: (e as Error).message }));
    }, 350);
    return () => window.clearTimeout(t);
  }, [key, job]);

  async function create() {
    setBusy(true);
    try {
      const r = await api<{ name: string; message: string }>(`/builder/jobs/${job}/create`, { method: "POST", json: { ...edits, run_after: runAfter } });
      onCreated(r.name, r.message);
    } catch (e) {
      setCheck({ yaml: "", error: (e as Error).message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Exhibit
      n={3}
      title="Review before saving"
      source={
        <>
          Draft by {draft.drafted_by}. Sources: {draft.counts.ok} verified, {draft.counts.dropped} dropped, {draft.counts.unverified} unverified. Region {draft.region}, timezone{" "}
          {draft.timezone}, languages {draft.language_hints.join(", ")}.
        </>
      }
    >
      {draft.warnings.length ? (
        <div style={{ display: "grid", gap: 8, marginBottom: 20 }}>
          {draft.warnings.map((w, i) => (
            <Note key={i} tone="warn">
              {w}
            </Note>
          ))}
        </div>
      ) : null}
      <div className="form-grid">
        <label className="field">
          <span>Name</span>
          <input className="input" value={display} maxLength={80} onChange={(e) => setDisplay(e.target.value)} />
        </label>
        <label className="field">
          <span>Workspace id</span>
          <input className="input" value={slug} onChange={(e) => setSlug(e.target.value)} aria-invalid={!!check?.error && check.error.includes("id")} />
          <span className="hint">Lowercase letters, digits, underscores.</span>
        </label>
        <label className="field">
          <span>Perspective</span>
          <input className="input" value={persp} maxLength={200} onChange={(e) => setPersp(e.target.value)} />
        </label>
        <label className="field">
          <span>Focal company</span>
          <input className="input" value={focal} placeholder="the company briefs are written for" onChange={(e) => setFocal(e.target.value)} />
        </label>

        <div className="field wide">
          <span>Companies, brands or technologies</span>
          <div className="rows-edit">
            {ents.map((e, i) => (
              <div className="row" key={i}>
                <input className="input" aria-label="Name" value={e.name} onChange={(ev) => setEnts(ents.map((x, j) => (j === i ? { ...x, name: ev.target.value } : x)))} />
                <input
                  className="input"
                  aria-label="Aliases, comma-separated"
                  placeholder="aliases, comma-separated"
                  value={e.aliases}
                  onChange={(ev) => setEnts(ents.map((x, j) => (j === i ? { ...x, aliases: ev.target.value } : x)))}
                />
                <button type="button" className="btn quiet sm" aria-label={`Remove ${e.name}`} onClick={() => setEnts(ents.filter((_, j) => j !== i))}>
                  <Minus size={14} />
                </button>
              </div>
            ))}
            <div>
              <button type="button" className="btn sm" onClick={() => setEnts([...ents, { name: "", aliases: "" }])}>
                <Plus size={14} /> Add an entity
              </button>
            </div>
          </div>
        </div>

        <div className="field wide">
          <span>Customer-voice aspects</span>
          <div className="rows-edit">
            {asps.map((a, i) => (
              <div className="row" key={i}>
                <input className="input" aria-label="Aspect" value={a.aspect} onChange={(ev) => setAsps(asps.map((x, j) => (j === i ? { ...x, aspect: ev.target.value } : x)))} />
                <input
                  className="input"
                  aria-label="Signal words, comma-separated"
                  placeholder="signal words, comma-separated"
                  value={a.words}
                  onChange={(ev) => setAsps(asps.map((x, j) => (j === i ? { ...x, words: ev.target.value } : x)))}
                />
                <button type="button" className="btn quiet sm" aria-label={`Remove ${a.aspect}`} onClick={() => setAsps(asps.filter((_, j) => j !== i))}>
                  <Minus size={14} />
                </button>
              </div>
            ))}
            <div>
              <button type="button" className="btn sm" onClick={() => setAsps([...asps, { aspect: "", words: "" }])}>
                <Plus size={14} /> Add an aspect
              </button>
            </div>
          </div>
        </div>

        <label className="field wide">
          <span>News search phrases (one per line; searched through the open GDELT news index)</span>
          <textarea className="textarea" rows={5} value={queries} onChange={(e) => setQueries(e.target.value)} />
        </label>
        <Checklist label="Verified RSS/Atom feeds" items={draft.rss_feeds} selected={feeds} onChange={setFeeds} />
        <Checklist label="Verified pages to watch for changes" items={[...pageLabels.keys()]} selected={pages} onChange={setPages} format={(u) => pageLabels.get(u) ?? u} />
        <Checklist label="Subreddits" items={draft.subreddits} selected={subs} onChange={setSubs} />
        <label className="field">
          <span>Reddit search terms (comma-separated)</span>
          <input className="input" value={rterms} onChange={(e) => setRterms(e.target.value)} />
        </label>
        <label className="field">
          <span>YouTube search terms (comma-separated)</span>
          <input className="input" value={yterms} onChange={(e) => setYterms(e.target.value)} />
        </label>
      </div>

      {draft.checks.length ? (
        <div style={{ marginTop: 18 }}>
          <Disclosure label={`Source checks (${draft.checks.length})`}>
            <div className="table-wrap" style={{ marginTop: 8 }}>
              <table className="table">
                <thead>
                  <tr>
                    <th>Kind</th>
                    <th>Status</th>
                    <th>Source</th>
                    <th>Detail</th>
                  </tr>
                </thead>
                <tbody>
                  {draft.checks.map((c, i) => (
                    <tr key={i}>
                      <td>{c.kind}</td>
                      <td>
                        <Mark kind={c.status === "ok" ? "verified" : c.status === "dropped" ? "disputed" : "thin"}>{c.status}</Mark>
                      </td>
                      <td className="clip" style={{ wordBreak: "break-all" }}>
                        {c.value}
                      </td>
                      <td className="muted">{c.detail}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Disclosure>
        </div>
      ) : null}

      <div style={{ marginTop: 18 }}>
        {check?.error ? (
          <ErrorNote>Fix before saving: {check.error}</ErrorNote>
        ) : check?.yaml ? (
          <Disclosure label="YAML preview">
            <pre className="log" style={{ marginTop: 8 }}>
              {check.yaml}
            </pre>
          </Disclosure>
        ) : null}
      </div>

      <div style={{ display: "grid", gap: 14, marginTop: 22 }}>
        <label className="check">
          <input type="checkbox" checked={runAfter} onChange={(e) => setRunAfter(e.target.checked)} />
          Start the first collection right after saving (delivery stays in the dry-run outbox)
        </label>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          <button type="button" className="btn primary" disabled={busy || !check || !!check.error} onClick={create}>
            {busy ? <Spinner /> : <Plus size={15} />} Create workspace
          </button>
          <button type="button" className="btn" onClick={onDiscard}>
            Discard draft
          </button>
        </div>
      </div>
    </Exhibit>
  );
}

const JOB_KEY = "fn-builder-job";

function NewWorkspace() {
  const { meta, reloadMeta } = useShell();
  const toast = useToast();
  const nav = useNavigate();
  const [desc, setDesc] = useState("");
  const [region, setRegion] = useState("");
  const [language, setLanguage] = useState("");
  const [persp, setPersp] = useState("");
  const [focal, setFocal] = useState("");
  const [verify, setVerify] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [job, setJob] = useState<string | null>(() => sessionStorage.getItem(JOB_KEY));
  const [status, setStatus] = useState<{ state: string; progress: string[]; error: string; draft?: Draft } | null>(null);
  const { data: lib } = useApi<Library>(`/library?q=__none__`);

  useEffect(() => {
    if (!job) return;
    try {
      sessionStorage.setItem(JOB_KEY, job);
    } catch {
      /* private mode: the draft survives only while this page is open */
    }
    let stop = false;
    const tick = async () => {
      try {
        const s = await api<{ state: string; progress: string[]; error: string; draft?: Draft }>(`/builder/jobs/${job}`);
        if (stop) return;
        setStatus(s);
        if (s.state === "running") window.setTimeout(tick, 700);
      } catch (e) {
        if (stop) return;
        setJob(null);
        setStatus(null);
        sessionStorage.removeItem(JOB_KEY);
        if ((e as Error).message) setErr((e as Error).message);
      }
    };
    void tick();
    return () => {
      stop = true;
    };
  }, [job]);

  async function start(payload: Record<string, unknown>) {
    setErr(null);
    setStatus(null);
    try {
      const r = await api<{ job: string }>(`/builder/draft`, { method: "POST", json: payload });
      setJob(r.job);
      window.setTimeout(() => document.getElementById("drafting")?.scrollIntoView({ behavior: "smooth", block: "start" }), 80);
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  const discard = () => {
    setJob(null);
    setStatus(null);
    sessionStorage.removeItem(JOB_KEY);
  };

  if (meta.readonly) return <Note>This dashboard is read-only (FIELDNOTE_DASHBOARD_READONLY). Use `fieldnote workspace new` instead.</Note>;
  const running = status?.state === "running" || (!!job && !status);

  return (
    <>
      <p className="page-deck" style={{ marginTop: 26 }}>
        Pick a topic from the library or describe any market, industry, set of companies, technology or topic. FieldNote drafts the entities, customer-voice aspects and news
        searches, then checks every proposed source before you save.
      </p>
      <Exhibit n={1} title={`Start from the library: ${meta.library_count} topics`} source={lib ? <>Drafting with the {lib.drafter}.</> : undefined}>
        <Disclosure label="Browse the topic library" defaultOpen={!job}>
          <div style={{ marginTop: 14 }}>
            <LibraryPicker busy={running} onUse={(id, r) => start({ template_id: id, region: r })} />
          </div>
        </Disclosure>
      </Exhibit>
      <Exhibit n={2} title="Or describe your own">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void start({ description: desc, region, language, perspective: persp, focal_company: focal, verify });
          }}
          style={{ display: "grid", gap: 16, maxWidth: 860 }}
        >
          <label className="field">
            <span>What do you want to track?</span>
            <textarea className="textarea" rows={4} maxLength={3000} value={desc} placeholder={EXAMPLES[0]} onChange={(e) => setDesc(e.target.value)} />
            <span className="hint">Examples: “{EXAMPLES[0]}” · “{EXAMPLES[1]}”</span>
          </label>
          <Disclosure label="Options">
            <div className="form-grid" style={{ marginTop: 12 }}>
              <label className="field">
                <span>Region</span>
                <input className="input" value={region} placeholder="inferred from the description, e.g. India or Global" onChange={(e) => setRegion(e.target.value)} />
              </label>
              <label className="field">
                <span>Language code</span>
                <input className="input" value={language} maxLength={5} placeholder="en" onChange={(e) => setLanguage(e.target.value)} />
              </label>
              <label className="field">
                <span>Perspective</span>
                <input className="input" value={persp} placeholder="market-level observer" onChange={(e) => setPersp(e.target.value)} />
              </label>
              <label className="field">
                <span>Focal company (optional)</span>
                <input className="input" value={focal} placeholder="the company briefs are written for" onChange={(e) => setFocal(e.target.value)} />
              </label>
              <label className="check wide">
                <input type="checkbox" checked={verify} onChange={(e) => setVerify(e.target.checked)} />
                Verify sources now (recommended; takes up to a minute)
              </label>
            </div>
          </Disclosure>
          <div>
            <button className="btn primary" disabled={running || desc.trim().length < 8}>
              {running ? <Spinner /> : <WandSparkles size={15} />} Draft workspace
            </button>
          </div>
        </form>
        {err ? (
          <div style={{ marginTop: 14 }}>
            <ErrorNote>{err}</ErrorNote>
          </div>
        ) : null}
      </Exhibit>

      <div id="drafting" />
      <Reveal open={!!job && status?.state !== "done"}>
        <div className="exhibit">
          <div className="ex-head">
            <span className="ex-title">{status?.state === "error" ? "Could not build a workspace" : "Building your workspace…"}</span>
          </div>
          <Progress lines={status?.progress ?? []} state={status?.state ?? "running"} />
          {status?.state === "error" ? (
            <div style={{ marginTop: 14, display: "grid", gap: 12 }}>
              <ErrorNote>{status.error}</ErrorNote>
              <div>
                <button type="button" className="btn" onClick={discard}>
                  Start again
                </button>
              </div>
            </div>
          ) : null}
        </div>
      </Reveal>

      {job && status?.state === "done" && status.draft ? (
        <Review
          key={job}
          job={job}
          draft={status.draft}
          onDiscard={discard}
          onCreated={(name, msg) => {
            sessionStorage.removeItem(JOB_KEY);
            toast(msg);
            invalidate("/");
            reloadMeta();
            nav(`/${name}/workspaces?tab=this`);
          }}
        />
      ) : null}
    </>
  );
}

// ---- this workspace ------------------------------------------------------------------------------

interface ConfigResp {
  yaml: string;
  source: string;
  has_file: boolean;
  has_row: boolean;
  entities: number;
  news_searches: number;
  feeds: number;
  pages: number;
}

const SOURCE_LABEL: Record<string, string> = { news: "News searches and feeds", pages: "Competitor pages", reddit: "Reddit", youtube: "YouTube" };

function RunPanel({ ws }: { ws: string }) {
  const { meta } = useShell();
  const { status, active, reload } = useLive();
  const toast = useToast();
  const now = useNow(active ? 1000 : 20000);
  const runs = useApi<{ busy: boolean; active: RunInfo | null; runs: RunInfo[]; log_name: string | null; log: string }>(`/ws/${ws}/run-status`);
  const [starting, setStarting] = useState<string | null>(null);
  const busy = !!active || !!runs.data?.busy;

  // While a run is going, keep its log tail current.
  useEffect(() => {
    if (!busy) return;
    const t = window.setInterval(() => void runs.reload(), 4000);
    return () => window.clearInterval(t);
  }, [busy]); // eslint-disable-line react-hooks/exhaustive-deps

  async function start(kind: "pulse" | "analysis") {
    setStarting(kind);
    try {
      await api(`/ws/${ws}/refresh`, { method: "POST", json: { kind } });
      toast(kind === "pulse" ? "Checking sources now" : "Full analysis started");
      await Promise.all([runs.reload(), reload()]);
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setStarting(null);
    }
  }

  const s = runs.data;
  const sch = status?.scheduler;
  return (
    <>
      <Exhibit
        n={1}
        first
        title={
          active
            ? `${active.kind === "pulse" ? "Collecting from live sources" : "Running the full analysis"}, ${elapsed(active.started_at, now)} so far.`
            : status?.updated_at
              ? `Sources last checked ${ago(status.updated_at, now)}; next check ${until(status.next_pulse, now)}.`
              : "No live collection has finished yet."
        }
        tools={
          <>
            <button type="button" className="btn" disabled={busy || !!starting || meta.readonly || meta.demo} onClick={() => start("pulse")}>
              {starting === "pulse" ? <Spinner /> : <RefreshCw size={15} />} Check sources now
            </button>
            <button type="button" className="btn primary" disabled={busy || !!starting || meta.readonly || meta.demo} onClick={() => start("analysis")}>
              {starting === "analysis" ? <Spinner /> : <Play size={15} />} Run full analysis
            </button>
          </>
        }
        source={
          sch ? (
            sch.enabled ? (
              <>
                Live schedule: sources every {sch.pulse_minutes} minutes, full analysis every {sch.analysis_hours} hours, the daily brief is
                delivered after {String(sch.deliver_hour).padStart(2, "0")}:00 local time when delivery channels are configured.{" "}
                {sch.in_process ? "Running inside this server." : "Run by a separate `fieldnote live` worker."}
              </>
            ) : (
              <>Live collection is switched off on this server (FIELDNOTE_SCHEDULER=off). Run `fieldnote live` as a worker, or start runs here.</>
            )
          ) : undefined
        }
        note={<>Checking sources collects and tags new items only. The full analysis also runs the researcher, critic, analyst and writer, which takes several minutes on free model tiers.</>}
      >
        {active ? (
          <div style={{ marginBottom: 20 }}>
            <Note>
              {active.kind === "pulse" ? "Collecting" : "Analysing"} since {fmtDateTime(active.started_at)} UTC.
              {active.log_line ? <span className="hint" style={{ display: "block", marginTop: 4, fontFamily: "var(--font-mono)" }}>{active.log_line}</span> : null}
            </Note>
            <div className="sk" style={{ height: 3, marginTop: 10 }} aria-hidden />
          </div>
        ) : null}
        {status ? (
          <div className="figures" style={{ marginBottom: 22 }}>
            <div className="figure">
              <div className="f-label">Items collected</div>
              <div className="f-value">{status.totals.documents}</div>
              <div className="f-sub">{status.totals.documents_24h} in the last 24 hours</div>
            </div>
            <div className="figure">
              <div className="f-label">Last check</div>
              <div className="f-value" style={{ fontSize: "var(--fs-24)" }}>{status.last_pulse ? ago(status.last_pulse.finished_at, now) : "Never"}</div>
              <div className="f-sub">
                {status.last_pulse ? `${status.last_pulse.status}${status.last_pulse.documents_new != null ? ` · ${status.last_pulse.documents_new} new` : ""}` : "–"}
              </div>
            </div>
            <div className="figure">
              <div className="f-label">Last analysis</div>
              <div className="f-value" style={{ fontSize: "var(--fs-24)" }}>{status.last_analysis ? ago(status.last_analysis.finished_at, now) : "Never"}</div>
              <div className="f-sub">{status.last_analysis ? `${status.last_analysis.status} · next ${until(status.next_analysis, now)}` : (until(status.next_analysis, now) === "due now" ? "due now" : `due ${until(status.next_analysis, now)}`)}</div>
            </div>
          </div>
        ) : (
          <Sk h={80} style={{ marginBottom: 22 }} />
        )}
        {status?.sources.length ? (
          <div className="table-wrap" style={{ marginBottom: 22 }}>
            <table className="table">
              <thead>
                <tr>
                  <th>Source</th>
                  <th>Status in the last check</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {status.sources.map((src) => (
                  <tr key={src.name}>
                    <td style={{ fontWeight: 620, color: "var(--ink-strong)" }}>{SOURCE_LABEL[src.name] ?? src.name}</td>
                    <td>
                      <Mark kind={src.status === "ok" ? "verified" : src.status === "failed" ? "disputed" : "stale"}>{src.status}</Mark>
                    </td>
                    <td className="muted">{src.message || (src.items != null ? `${src.items} items` : "")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      {!s ? (
          <Sk h={120} />
        ) : s.runs.length ? (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Run</th>
                  <th>Date</th>
                  <th>Kind</th>
                  <th>Status</th>
                  <th>Started (UTC)</th>
                  <th className="r">Minutes</th>
                  <th className="r">New documents</th>
                  <th className="r">LLM cost</th>
                </tr>
              </thead>
              <tbody>
                {s.runs.map((r) => (
                  <tr key={r.id}>
                    <td className="tabular">#{r.id}</td>
                    <td>{r.run_date}</td>
                    <td>{r.kind === "daily" ? "analysis" : r.kind}</td>
                    <td>
                      <Mark kind={r.status === "success" ? "verified" : r.status === "failed" ? "disputed" : "thin"}>{r.status}</Mark>
                    </td>
                    <td className="muted">{fmtDateTime(r.started_at)}</td>
                    <td className="r">
                      {r.finished_at && r.started_at ? ((new Date(r.finished_at).getTime() - new Date(r.started_at).getTime()) / 60000).toFixed(1) : ""}
                    </td>
                    <td className="r">{String(r.stats?.documents_new ?? "")}</td>
                    <td className="r">${r.cost_usd.toFixed(4)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="hint">No runs yet.</p>
        )}
        {s?.log_name ? (
          <div style={{ marginTop: 12 }}>
            <Disclosure label={`Latest run log (${s.log_name})`} defaultOpen={busy}>
              <pre className="log" style={{ marginTop: 8 }}>
                {s.log || "(empty)"}
              </pre>
            </Disclosure>
          </div>
        ) : null}
        </Exhibit>
    </>
  );
}

function ThisWorkspace() {
  const { ws, ctx, meta, reloadCtx } = useShell();
  const toast = useToast();
  const cfg = useApi<ConfigResp>(ws ? `/ws/${ws}/config` : null);
  const [text, setText] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  if (!ws || !ctx) return <Empty title="No workspace selected yet." hint="Create one in the first tab." />;
  const d = cfg.data;
  const value = text ?? d?.yaml ?? "";
  const dirty = !!d && text !== null && text !== d.yaml;

  async function save() {
    setSaving(true);
    setErr(null);
    try {
      const r = await api<{ message: string }>(`/ws/${ws}/config`, { method: "PUT", json: { yaml: value } });
      toast(r.message);
      setText(null);
      await cfg.reload();
      reloadCtx();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  const stored = d ? [d.has_file ? `workspaces/${ws}.yaml` : "", d.has_row ? "the database" : ""].filter(Boolean).join(" and ") : "";
  return (
    <>
      <p className="page-deck" style={{ marginTop: 26 }}>
        <b style={{ color: "var(--ink-strong)" }}>{ctx.display_name}</b> · id <code>{ws}</code>
        {d ? (
          <>
            {" "}
            · saved in {stored || d.source} (active copy: {d.source}) · {d.entities} entities · {d.news_searches} news searches · {d.feeds} feeds · {d.pages} watched pages
          </>
        ) : null}
      </p>
      <RunPanel ws={ws} />
      <Exhibit
        n={2}
        title="Configuration"
        tools={
          <a className="btn sm" href={`data:application/x-yaml;charset=utf-8,${encodeURIComponent(d?.yaml ?? "")}`} download={`${ws}.yaml`}>
            <Download size={14} /> Download YAML
          </a>
        }
        note={<>Validated before saving; the id cannot change here. Run `fieldnote doctor -w {ws}` to re-check new URLs.</>}
      >
        {!d ? (
          <Sk h={300} />
        ) : meta.readonly ? (
          <pre className="log" style={{ maxHeight: 520 }}>
            {d.yaml}
          </pre>
        ) : (
          <div style={{ display: "grid", gap: 12 }}>
            <label className="field">
              <span className="sr-only">Workspace YAML</span>
              <textarea className="textarea code" rows={22} spellCheck={false} value={value} onChange={(e) => setText(e.target.value)} />
            </label>
            {err ? <ErrorNote>{err}</ErrorNote> : null}
            <div style={{ display: "flex", gap: 10 }}>
              <button type="button" className="btn primary" disabled={!dirty || saving} onClick={save}>
                {saving ? <Spinner /> : <Save size={15} />} Save changes
              </button>
              <button type="button" className="btn" disabled={!dirty} onClick={() => setText(null)}>
                Revert
              </button>
            </div>
          </div>
        )}
      </Exhibit>
    </>
  );
}

// ---- all workspaces ------------------------------------------------------------------------------

interface AllResp {
  workspaces: { name: string; display_name: string; storage: string; source: string; status: string; last_run: { date: string; status: string } | null; problem: string }[];
}

function AllWorkspaces() {
  const { meta, reloadMeta } = useShell();
  const toast = useToast();
  const nav = useNavigate();
  const all = useApi<AllResp>("/workspaces");
  const [arch, setArch] = useState("");
  const [sure, setSure] = useState(false);
  const [back, setBack] = useState("");
  const [file, setFile] = useState<{ name: string; text: string } | null>(null);
  const [over, setOver] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const rows = all.data?.workspaces ?? [];
  const active = rows.filter((r) => r.status === "active");
  const archived = rows.filter((r) => r.status === "archived");

  async function act(path: string, json?: unknown, then?: (r: { message: string; name?: string }) => void) {
    setErr(null);
    try {
      const r = await api<{ message: string; name?: string }>(path, { method: "POST", json });
      toast(r.message);
      invalidate("/");
      await all.reload();
      reloadMeta();
      then?.(r);
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  if (!all.data) return <Sk h={200} style={{ marginTop: 30 }} />;
  return (
    <>
      <Exhibit n={1} first title={`${rows.length} workspaces`} source={<>Workspaces live as YAML files under workspaces/ and, when shared with a hosted dashboard, in the database.</>}>
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Id</th>
                <th>Stored in</th>
                <th>Status</th>
                <th>Last run</th>
                <th>Problem</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.name} className={r.status === "active" ? "selectable" : undefined} onClick={() => r.status === "active" && nav(`/${r.name}/overview`)}>
                  <td style={{ fontWeight: 620, color: "var(--ink-strong)" }}>{r.display_name}</td>
                  <td>
                    <code>{r.name}</code>
                  </td>
                  <td className="muted">{r.storage}</td>
                  <td>
                    <Mark kind={r.status === "active" ? "verified" : r.status === "invalid" ? "disputed" : "stale"}>{r.status}</Mark>
                  </td>
                  <td className="muted">{r.last_run ? `${r.last_run.date} (${r.last_run.status})` : "–"}</td>
                  <td className="muted clip">{r.problem}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Exhibit>
      {err ? (
        <div style={{ marginTop: 20 }}>
          <ErrorNote>{err}</ErrorNote>
        </div>
      ) : null}
      {meta.readonly ? null : (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))", columnGap: 40 }}>
          <Exhibit n={2} title="Archive a workspace" note={<>Hides it from lists and scheduled runs. Collected data is kept; restore any time.</>}>
            <div style={{ display: "grid", gap: 12 }}>
              <select className="select" value={arch} onChange={(e) => setArch(e.target.value)} aria-label="Workspace to archive">
                <option value="">Choose a workspace…</option>
                {active.map((r) => (
                  <option key={r.name} value={r.name}>
                    {r.display_name}
                  </option>
                ))}
              </select>
              <label className="check">
                <input type="checkbox" checked={sure} onChange={(e) => setSure(e.target.checked)} />
                Yes, archive it
              </label>
              <div>
                <button
                  type="button"
                  className="btn danger"
                  disabled={!arch || !sure}
                  onClick={() =>
                    act(`/workspaces/${arch}/archive`, undefined, () => {
                      setArch("");
                      setSure(false);
                    })
                  }
                >
                  <Archive size={15} /> Archive
                </button>
              </div>
            </div>
          </Exhibit>
          <Exhibit n={3} title="Restore a workspace" note={<>Brings an archived workspace back into lists and scheduled runs.</>}>
            <div style={{ display: "grid", gap: 12 }}>
              <select className="select" value={back} onChange={(e) => setBack(e.target.value)} aria-label="Archived workspace">
                <option value="">{archived.length ? "Choose an archived workspace…" : "Nothing is archived"}</option>
                {archived.map((r) => (
                  <option key={r.name} value={r.name}>
                    {r.display_name}
                  </option>
                ))}
              </select>
              <div>
                <button type="button" className="btn" disabled={!back} onClick={() => act(`/workspaces/${back}/restore`, undefined, () => nav(`/${back}/overview`))}>
                  <ArchiveRestore size={15} /> Restore
                </button>
              </div>
            </div>
          </Exhibit>
          <Exhibit n={4} title="Import a workspace YAML" note={<>Limit 200 KB. Run `fieldnote doctor -w &lt;id&gt;` afterwards to check its URLs.</>}>
            <div style={{ display: "grid", gap: 12 }}>
              <label className="field">
                <span>Workspace file</span>
                <input
                  className="input"
                  type="file"
                  accept=".yaml,.yml"
                  style={{ paddingTop: 6 }}
                  onChange={async (e) => {
                    const f = e.target.files?.[0];
                    if (!f) return setFile(null);
                    if (f.size > 200_000) {
                      setErr("That file is too large for a workspace config (limit 200 KB).");
                      return setFile(null);
                    }
                    setFile({ name: f.name, text: await f.text() });
                  }}
                />
              </label>
              <label className="check">
                <input type="checkbox" checked={over} onChange={(e) => setOver(e.target.checked)} />
                Replace an existing workspace with the same id
              </label>
              <div>
                <button type="button" className="btn" disabled={!file} onClick={() => act("/workspaces/import", { yaml: file?.text, overwrite: over }, (r) => r.name && nav(`/${r.name}/overview`))}>
                  <Upload size={15} /> Import
                </button>
              </div>
            </div>
          </Exhibit>
        </div>
      )}
    </>
  );
}

export default function Workspaces() {
  const { ws, ctx } = useShell();
  const [params, setParams] = useSearchParams();
  const tab = (TABS.find((t) => t.id === params.get("tab"))?.id ?? (ws ? "this" : "new")) as Tab;
  if (ws && !ctx) return <PageSkeleton />;
  return (
    <>
      <PageHead
        title={ws && ctx ? `Set up ${ctx.display_name}, or start tracking something new.` : "Describe what you want to track to create your first workspace."}
        deck="A workspace is a market, set of companies or topic FieldNote watches: its entities, sources, customer-voice aspects and scoring weights."
      />
      <LayoutGroup id="ws-tabs">
        <div className="tabs" role="tablist">
          {TABS.map((t) => (
            <button key={t.id} type="button" role="tab" className="tab" aria-selected={t.id === tab} onClick={() => setParams({ tab: t.id }, { replace: true })}>
              {t.label}
              {t.id === tab ? <motion.span layoutId="tab-line" className="tab-line" transition={{ type: "spring", stiffness: 520, damping: 42 }} /> : null}
            </button>
          ))}
        </div>
      </LayoutGroup>
      <motion.div key={tab} initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.22, ease: EASE }}>
        {tab === "new" ? <NewWorkspace /> : tab === "this" ? <ThisWorkspace /> : <AllWorkspaces />}
      </motion.div>
    </>
  );
}
