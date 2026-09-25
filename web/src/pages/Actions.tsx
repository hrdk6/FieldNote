import { useRef, useState } from "react";
import { BellRing, Download, Sparkles } from "lucide-react";
import { api, invalidate, useApi, type ActionItem } from "../api";
import { Empty, ErrorNote, Exhibit, Mark, Note, PageHead, PageSkeleton, Reveal, Seg, Spinner, useToast } from "../components/ui";
import { useShell } from "../shell";
import { fmtDate, plural } from "../util";

interface Resp {
  items: ActionItem[];
  statuses: string[];
  priorities: string[];
  today: string;
}

interface Preview {
  token: string;
  via: string;
  note_date: string;
  items: { description: string; owner: string; due_date: string | null; priority: string; confidence: number; flags: string[]; source_sentence: string }[];
  merges: { index: number; existing_id: number; existing_description: string; score: number }[];
}

const FILTERS = ["open", "done", "dropped", "all"] as const;

function ItemRow({ it, ws, today, priorities, statuses, onSaved }: { it: ActionItem; ws: string; today: string; priorities: string[]; statuses: string[]; onSaved: (it: ActionItem) => void }) {
  const toast = useToast();
  const row = useRef<HTMLTableRowElement>(null);
  const [owner, setOwner] = useState(it.owner);
  async function save(patch: Record<string, string>) {
    try {
      const next = await api<ActionItem>(`/ws/${ws}/actions/${it.id}`, { method: "PATCH", json: patch });
      onSaved(next);
      const el = row.current;
      if (el && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
        const c = getComputedStyle(document.documentElement).getPropertyValue("--verified-soft").trim();
        el.animate([{ backgroundColor: c }, { backgroundColor: "transparent" }], { duration: 1100, easing: "cubic-bezier(0.16, 1, 0.3, 1)" });
      }
      toast(`Saved action #${it.id}`);
    } catch (e) {
      toast((e as Error).message, "error");
    }
  }
  const overdue = it.status === "open" && it.due_date && it.due_date < today;
  return (
    <tr ref={row}>
      <td className="muted tabular">#{it.id}</td>
      <td style={{ minWidth: 260 }}>
        <div style={{ color: "var(--ink-strong)", fontWeight: 560 }}>{it.description}</div>
        {it.source_sentence ? <div className="hint" style={{ marginTop: 3 }}>From the notes: “{it.source_sentence}”</div> : null}
        {it.flags ? (
          <div className="marks" style={{ marginTop: 6 }}>
            {it.flags.split(", ").map((f) => (
              <Mark key={f} kind="thin">
                {f.replace(/_/g, " ")}
              </Mark>
            ))}
          </div>
        ) : null}
      </td>
      <td style={{ width: 150 }}>
        <input
          className="input"
          aria-label={`Owner of action ${it.id}`}
          value={owner}
          onChange={(e) => setOwner(e.target.value)}
          onBlur={() => owner !== it.owner && save({ owner })}
          onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
        />
      </td>
      <td style={{ width: 160 }}>
        <input
          className="input"
          type="date"
          aria-label={`Due date of action ${it.id}`}
          aria-invalid={!!overdue}
          defaultValue={it.due_date}
          onBlur={(e) => e.target.value !== it.due_date && save({ due_date: e.target.value })}
        />
        {overdue ? <div className="hint" style={{ color: "var(--disputed)", marginTop: 3 }}>Overdue</div> : null}
      </td>
      <td style={{ width: 120 }}>
        <select className="select" aria-label={`Priority of action ${it.id}`} value={it.priority} onChange={(e) => save({ priority: e.target.value })}>
          {priorities.map((p) => (
            <option key={p}>{p}</option>
          ))}
        </select>
      </td>
      <td style={{ width: 120 }}>
        <select className="select" aria-label={`Status of action ${it.id}`} value={it.status} onChange={(e) => save({ status: e.target.value })}>
          {statuses.map((p) => (
            <option key={p}>{p}</option>
          ))}
        </select>
      </td>
    </tr>
  );
}

function Notes({ ws, today, onSaved }: { ws: string; today: string; onSaved: () => void }) {
  const toast = useToast();
  const [title, setTitle] = useState("");
  const [date, setDate] = useState(today);
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [prev, setPrev] = useState<Preview | null>(null);
  const [keep, setKeep] = useState<boolean[]>([]);
  const [merge, setMerge] = useState(true);

  async function extract(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      const p = await api<Preview>(`/ws/${ws}/notes/preview`, { method: "POST", json: { title, date, body } });
      setPrev(p);
      setKeep(p.items.map(() => true));
    } catch (ex) {
      setErr((ex as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function commit() {
    if (!prev) return;
    setBusy(true);
    try {
      const r = await api<{ created: number[]; merged: number[] }>(`/ws/${ws}/notes/commit`, {
        method: "POST",
        json: { token: prev.token, selected: keep.map((k, i) => (k ? i : -1)).filter((i) => i >= 0), accept_merges: merge },
      });
      toast(`Saved: ${r.created.length} new, ${r.merged.length} merged`);
      setPrev(null);
      setBody("");
      setTitle("");
      onSaved();
    } catch (ex) {
      setErr((ex as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <form onSubmit={extract} className="form-grid" style={{ maxWidth: 860 }}>
        <label className="field">
          <span>Title</span>
          <input className="input" value={title} placeholder="Weekly market sync" onChange={(e) => setTitle(e.target.value)} />
        </label>
        <label className="field">
          <span>Meeting date</span>
          <input className="input" type="date" value={date} onChange={(e) => setDate(e.target.value)} />
        </label>
        <label className="field wide">
          <span>Paste notes</span>
          <textarea
            className="textarea"
            rows={8}
            value={body}
            placeholder={"Attendees: ...\n- Priya will prepare the pricing comparison by Friday."}
            onChange={(e) => setBody(e.target.value)}
          />
          <span className="hint">Relative dates (“by Friday”) are resolved against the meeting date. Nothing is saved until you confirm.</span>
        </label>
        <div className="wide">
          <button className="btn primary" disabled={busy || !body.trim()}>
            {busy && !prev ? <Spinner /> : <Sparkles size={15} />} Extract action items
          </button>
        </div>
      </form>
      {err ? (
        <div style={{ marginTop: 14 }}>
          <ErrorNote>{err}</ErrorNote>
        </div>
      ) : null}
      <Reveal open={!!prev}>
        {prev ? (
          <div style={{ marginTop: 28 }}>
            <p style={{ marginBottom: 12 }}>
              <b>{plural(prev.items.length, "item")} found</b> <span className="hint">({prev.via}; dates resolved against {fmtDate(prev.note_date)})</span>
            </p>
            {prev.items.length ? (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Keep</th>
                      <th>Description</th>
                      <th>Owner</th>
                      <th>Due</th>
                      <th>Priority</th>
                      <th className="r">Confidence</th>
                    </tr>
                  </thead>
                  <tbody>
                    {prev.items.map((it, i) => (
                      <tr key={i}>
                        <td>
                          <input
                            type="checkbox"
                            aria-label={`Keep item ${i + 1}`}
                            checked={keep[i] ?? true}
                            onChange={(e) => setKeep((k) => k.map((x, j) => (j === i ? e.target.checked : x)))}
                            style={{ accentColor: "var(--navy)", width: 15, height: 15 }}
                          />
                        </td>
                        <td>
                          {it.description}
                          {it.flags.length ? (
                            <div className="marks" style={{ marginTop: 5 }}>
                              {it.flags.map((f) => (
                                <Mark key={f} kind="thin">
                                  {f.replace(/_/g, " ")}
                                </Mark>
                              ))}
                            </div>
                          ) : null}
                        </td>
                        <td>{it.owner}</td>
                        <td>{it.due_date ? fmtDate(it.due_date) : <span className="muted">none</span>}</td>
                        <td>{it.priority}</td>
                        <td className="r">{it.confidence.toFixed(2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <Note>No action items were found in these notes.</Note>
            )}
            {prev.merges.length ? (
              <div style={{ marginTop: 14, display: "grid", gap: 10 }}>
                <Note tone="warn">
                  Some items look like existing open items:{" "}
                  {prev.merges.map((m) => `item ${m.index + 1} ≈ #${m.existing_id} (${Math.round(m.score)}%)`).join("; ")}.
                </Note>
                <label className="check">
                  <input type="checkbox" checked={merge} onChange={(e) => setMerge(e.target.checked)} />
                  Merge them into the existing items instead of creating duplicates
                </label>
              </div>
            ) : null}
            <div style={{ display: "flex", gap: 10, marginTop: 18 }}>
              <button type="button" className="btn primary" onClick={commit} disabled={busy || !keep.some(Boolean)}>
                {busy ? <Spinner /> : null} Confirm and save
              </button>
              <button type="button" className="btn" onClick={() => setPrev(null)}>
                Discard
              </button>
            </div>
          </div>
        ) : null}
      </Reveal>
    </>
  );
}

export default function Actions() {
  const { ws } = useShell();
  const toast = useToast();
  const [show, setShow] = useState<(typeof FILTERS)[number]>("open");
  const { data, error, reload, setData } = useApi<Resp>(`/ws/${ws}/actions?status=${show}`);
  const [rem, setRem] = useState<{ text: string; deliveries: { channel: string; status: string; message: string }[]; skipped: number } | null>(null);
  const [remBusy, setRemBusy] = useState(false);

  if (error) return <ErrorNote>{error}</ErrorNote>;
  if (!data) return <PageSkeleton figures={false} />;
  const items = data.items;
  const open = items.filter((i) => i.status === "open");
  const overdue = open.filter((i) => i.due_date && i.due_date < data.today).sort((a, b) => a.due_date.localeCompare(b.due_date));
  const title =
    show === "open"
      ? open.length
        ? `${plural(open.length, "open action")}${overdue.length ? `; ${overdue.length} overdue, the oldest due ${fmtDate(overdue[0].due_date)}` : ", none overdue"}.`
        : "No open actions."
      : `${plural(items.length, `${show === "all" ? "" : `${show} `}action`)}.`;

  async function reminders() {
    setRemBusy(true);
    try {
      setRem(await api(`/ws/${ws}/actions/reminders`, { method: "POST" }));
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setRemBusy(false);
    }
  }

  return (
    <>
      <PageHead title={title} deck="Action items extracted from meeting notes. Edit owner, due date, priority or status in place; changes save as you make them." />

      <Exhibit
        n={1}
        first
        title="Action tracker"
        tools={
          <>
            <Seg label="Status" value={show} options={FILTERS} onChange={setShow} format={(s) => s[0].toUpperCase() + s.slice(1)} />
            <a className="btn sm" href={`/api/ws/${ws}/actions.csv`} download>
              <Download size={14} /> Export CSV
            </a>
            <button type="button" className="btn sm" onClick={reminders} disabled={remBusy}>
              {remBusy ? <Spinner /> : <BellRing size={14} />} Run reminders
            </button>
          </>
        }
        note={<>Reminders go to the dry-run outbox unless delivery is configured for this workspace.</>}
      >
        <Reveal open={!!rem}>
          {rem ? (
            <div style={{ marginBottom: 20, display: "grid", gap: 10 }}>
              {rem.text ? (
                <>
                  <pre className="log">{rem.text}</pre>
                  {rem.deliveries.map((d) => (
                    <p key={d.channel} className="hint">
                      {d.channel}: {d.status} – {d.message}
                    </p>
                  ))}
                </>
              ) : (
                <Note>Nothing to remind ({rem.skipped} already reminded today).</Note>
              )}
            </div>
          ) : null}
        </Reveal>
        {items.length ? (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Action</th>
                  <th>Owner</th>
                  <th>Due</th>
                  <th>Priority</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {items.map((it) => (
                  <ItemRow
                    key={it.id}
                    it={it}
                    ws={ws!}
                    today={data.today}
                    priorities={data.priorities}
                    statuses={data.statuses}
                    onSaved={(next) => {
                      invalidate(`/ws/${ws}/overview`);
                      setData({ ...data, items: data.items.map((x) => (x.id === next.id ? next : x)) });
                    }}
                  />
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty title="No action items here." hint="Paste meeting notes below; FieldNote extracts owners, due dates and priorities and asks before saving." />
        )}
      </Exhibit>

      <Exhibit n={2} title="Add meeting notes" source={<>Extraction runs on the configured LLM (or the rule-based extractor offline); similar open items are offered for merging.</>}>
        <Notes
          ws={ws!}
          today={data.today}
          onSaved={() => {
            invalidate(`/ws/${ws}/`);
            void reload();
          }}
        />
      </Exhibit>
    </>
  );
}
