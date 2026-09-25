import { useMemo, useState } from "react";
import { ChevronRight, ExternalLink } from "lucide-react";
import { useApi, type Change, type DiffSeg } from "../api";
import { KindGlyph, Timeline } from "../components/charts";
import { Empty, ErrorNote, Exhibit, PageHead, PageSkeleton, Reveal, Seg } from "../components/ui";
import { useShell } from "../shell";
import { cap, entityColor, entityOrder, fmtDate, parseDate, plural } from "../util";

const KINDS = ["price", "feature", "launch", "policy", "dealer", "other"];
const WINDOWS = ["7", "14", "30", "90", "365"] as const;

function Segs({ segs }: { segs: DiffSeg[] }) {
  if (!segs.length) return <i className="hint">(not present)</i>;
  return (
    <>
      {segs.map(([op, text], i) =>
        op === "del" ? <del key={i}>{text}</del> : op === "ins" ? <ins key={i}>{text}</ins> : <span key={i}>{text}</span>,
      )}
    </>
  );
}

function FeedRow({ c, color, open, onToggle }: { c: Change; color: string; open: boolean; onToggle: () => void }) {
  return (
    <div className="feed-row" id={`change-${c.id}`}>
      <button type="button" className="feed-head" aria-expanded={open} onClick={onToggle}>
        <span className="f-meta">
          <span className="tabular" style={{ color: "var(--ink-3)", fontSize: "var(--fs-13)" }}>
            {fmtDate(c.detected_at)}
          </span>
          <span className="entity">
            <i className="swatch" style={{ background: color }} />
            {c.competitor}
          </span>
          <span className="mark" style={{ justifySelf: "start" }}>
            <KindGlyph kind={c.kind} color={color} />
            {cap(c.kind)}
          </span>
        </span>
        <span className="f-sum" style={{ minWidth: 0 }}>
          {c.summary}
        </span>
        <span className="sig hide-sm" title="Significance (0 to 1)">
          <span className="tabular">{c.significance.toFixed(2)}</span>
          <i style={{ ["--w" as string]: `${Math.round(c.significance * 100)}%` }} />
        </span>
        <ChevronRight size={16} className="chev" />
      </button>
      <Reveal open={open}>
        <div className="feed-body">
          <a href={c.url} target="_blank" rel="noreferrer" style={{ display: "inline-flex", gap: 6, alignItems: "center", fontSize: "var(--fs-13)" }}>
            Open the page <ExternalLink size={13} />
          </a>
          <span className="hint" style={{ marginLeft: 12 }}>
            Detected in run #{c.run_id ?? "–"} · significance {c.significance.toFixed(2)}
          </span>
          <div className="diff">
            <div>
              <h5>Before</h5>
              <pre>
                <Segs segs={c.before} />
              </pre>
            </div>
            <div>
              <h5>After</h5>
              <pre>
                <Segs segs={c.after} />
              </pre>
            </div>
          </div>
        </div>
      </Reveal>
    </div>
  );
}

export default function Market() {
  const { ws, ctx } = useShell();
  const [days, setDays] = useState<(typeof WINDOWS)[number]>("30");
  const [kinds, setKinds] = useState<string[]>(KINDS);
  const [comps, setComps] = useState<string[] | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const { data, error } = useApi<{ days: number; changes: Change[]; pages_watched: number }>(`/ws/${ws}/changes?days=${days}`);

  const entities = useMemo(() => entityOrder(ctx?.competitors ?? [], (data?.changes ?? []).map((c) => c.competitor)), [ctx, data]);
  const selected = comps ?? entities;
  const rows = useMemo(
    () =>
      (data?.changes ?? [])
        .filter((c) => kinds.includes(c.kind) && selected.includes(c.competitor))
        .sort((a, b) => b.significance - a.significance || (parseDate(b.detected_at)?.getTime() ?? 0) - (parseDate(a.detected_at)?.getTime() ?? 0)),
    [data, kinds, selected],
  );

  if (error) return <ErrorNote>{error}</ErrorNote>;
  if (!data || !ctx) return <PageSkeleton figures={false} />;

  const byComp = new Map<string, number>();
  rows.forEach((c) => byComp.set(c.competitor, (byComp.get(c.competitor) ?? 0) + 1));
  const leader = [...byComp.entries()].sort((a, b) => b[1] - a[1])[0];
  const byKind = new Map<string, number>();
  rows.forEach((c) => byKind.set(c.kind, (byKind.get(c.kind) ?? 0) + 1));
  const topKind = [...byKind.entries()].sort((a, b) => b[1] - a[1])[0];
  const to = parseDate(ctx.now) ?? new Date();
  const from = new Date(to.getTime() - Number(days) * 86400000);
  const rowsWithChanges = entities.filter((e) => selected.includes(e) && byComp.has(e));
  const tied = [...byComp.values()].filter((v) => v === leader?.[1]).length > 1;
  const title = rows.length
    ? `${plural(rows.length, "competitor page change")} in the last ${days} days; ${
        tied ? `spread across ${plural(byComp.size, "competitor")}` : `${leader[0]} changed most (${leader[1]})`
      }.`
    : `No competitor page changes in the last ${days} days.`;

  const toggle = (list: string[], v: string) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);

  return (
    <>
      <PageHead
        title={title}
        deck={
          rows.length
            ? `${topKind ? `${cap(topKind[0])} changes lead (${topKind[1]}).` : ""} ${data.pages_watched} competitor pages are watched and diffed against their previous snapshot on every run.`
            : `${data.pages_watched} competitor pages are watched. Pages are diffed against their previous snapshot on every run, and small diffs are ignored unless a price or figure changed.`
        }
      />

      <div className="toolbar" style={{ marginTop: 28 }}>
        <div className="field">
          <span>Window</span>
          <Seg label="Window" value={days} options={WINDOWS} onChange={setDays} format={(d) => `${d} days`} />
        </div>
        <div className="field">
          <span>Change type</span>
          <div className="chips">
            {KINDS.map((k) => (
              <button key={k} type="button" className="chip" aria-pressed={kinds.includes(k)} onClick={() => setKinds((l) => toggle(l, k))}>
                {cap(k)}
              </button>
            ))}
          </div>
        </div>
      </div>
      <div className="field" style={{ marginTop: 14 }}>
        <span>Competitors</span>
        <div className="chips">
          {entities.map((e) => (
            <button key={e} type="button" className="chip" aria-pressed={selected.includes(e)} onClick={() => setComps(toggle(selected, e))}>
              <i className="swatch" style={{ background: entityColor(entities, e), marginRight: 7 }} />
              {e}
            </button>
          ))}
        </div>
      </div>

      <Exhibit
        n={1}
        title={(() => {
          if (!rows.length) return "When each competitor changed its pages.";
          const dates = new Set(rows.map((c) => fmtDate(c.detected_at)));
          const top = rows[0];
          return dates.size === 1
            ? `All ${plural(rows.length, "change")} landed on ${[...dates][0]}; the largest was ${top.competitor}'s ${top.kind} change.`
            : `The largest change was ${top.competitor}'s ${top.kind} change on ${fmtDate(top.detected_at)}.`;
        })()}
        source={<>Page snapshots from every run in the window; dot size is the change's significance (0 to 1).</>}
        note={
          <span style={{ display: "inline-flex", flexWrap: "wrap", gap: "4px 14px", verticalAlign: "middle" }}>
            {KINDS.map((k) => (
              <span key={k} style={{ display: "inline-flex", gap: 5, alignItems: "center" }}>
                <KindGlyph kind={k} color="var(--ink-3)" /> {k}
              </span>
            ))}
          </span>
        }
      >
        {rows.length ? (
          <Timeline
            rows={rowsWithChanges}
            from={from}
            to={to}
            onPick={(id) => {
              setOpen(id);
              window.setTimeout(() => document.getElementById(`change-${id}`)?.scrollIntoView({ behavior: "smooth", block: "center" }), 60);
            }}
            dots={rows.map((c) => ({
              id: c.id,
              row: c.competitor,
              date: c.detected_at,
              size: c.significance,
              kind: c.kind,
              color: entityColor(entities, c.competitor),
              tip: (
                <div>
                  <b>
                    {c.competitor} · {c.kind}
                  </b>
                  <div>{c.summary}</div>
                  <div className="hint">
                    {fmtDate(c.detected_at)} · significance {c.significance.toFixed(2)} · click to open the diff
                  </div>
                </div>
              ),
            }))}
          />
        ) : (
          <Empty title="Nothing changed on the watched pages in this window." hint="Widen the window or include more change types and competitors." />
        )}
      </Exhibit>

      {rows.length ? (
        <Exhibit
          n={2}
          title={`${rows[0].competitor}'s ${rows[0].kind} change is the most significant (${rows[0].significance.toFixed(2)}); ${rows.length} changes, most significant first.`}
          source={<>Rule-based classification (price delta, launch words, policy, dealer, feature); the fast model only sees what the rules could not place.</>}
        >
          <div className="feed">
            {rows.map((c) => (
              <FeedRow
                key={c.id}
                c={c}
                color={entityColor(entities, c.competitor)}
                open={open === c.id}
                onToggle={() => setOpen((o) => (o === c.id ? null : c.id))}
              />
            ))}
          </div>
        </Exhibit>
      ) : null}
    </>
  );
}
