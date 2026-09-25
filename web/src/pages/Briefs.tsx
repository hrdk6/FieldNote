import { useMemo, useState } from "react";
import { Download, Send } from "lucide-react";
import { api, useApi, type BriefRow } from "../api";
import { Empty, ErrorNote, Exhibit, Mark, Note, PageHead, PageSkeleton, Reveal, Seg, Sk, Spinner, useToast } from "../components/ui";
import { useShell } from "../shell";
import { renderMarkdown } from "../markdown";
import { fmtDateTime } from "../util";

const KINDS = ["daily", "weekly", "ask"] as const;
type Kind = (typeof KINDS)[number];
const KIND_LABEL: Record<Kind, string> = { daily: "Daily", weekly: "Weekly", ask: "Ask answers" };

interface Resp {
  briefs: BriefRow[];
  channels: string[];
  dry_run_reasons: string[];
}

function Doc({ ws, id }: { ws: string; id: number }) {
  const { data, error } = useApi<{ markdown: string }>(`/ws/${ws}/briefs/${id}`);
  const html = useMemo(() => (data?.markdown ? renderMarkdown(data.markdown) : ""), [data]);
  if (error) return <ErrorNote>{error}</ErrorNote>;
  if (!data)
    return (
      <div>
        <Sk w="60%" h={22} />
        {[...Array(6)].map((_, i) => (
          <Sk key={i} w={`${92 - i * 6}%`} style={{ marginTop: 12 }} />
        ))}
      </div>
    );
  if (!html) return <Note>This brief has no Markdown version; use the download buttons above.</Note>;
  return <article className="prose" dangerouslySetInnerHTML={{ __html: html }} />;
}

export default function Briefs() {
  const { ws } = useShell();
  const toast = useToast();
  const [kind, setKind] = useState<Kind>("daily");
  const { data, error } = useApi<Resp>(`/ws/${ws}/briefs?kind=${kind}`);
  const [sel, setSel] = useState<number | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [sending, setSending] = useState(false);
  const [results, setResults] = useState<{ channel: string; status: string; message: string }[] | null>(null);

  if (error) return <ErrorNote>{error}</ErrorNote>;
  if (!data) return <PageSkeleton figures={false} />;
  const briefs = data.briefs;
  const cur = briefs.find((b) => b.id === sel) ?? briefs[0];
  const where = data.channels.length ? data.channels.join(" and ") : "the dry-run outbox";

  async function send() {
    if (!cur) return;
    setSending(true);
    try {
      const r = await api<{ results: { channel: string; status: string; message: string }[] }>(`/ws/${ws}/briefs/${cur.id}/send`, { method: "POST" });
      setResults(r.results);
      setConfirm(false);
      toast(data!.channels.length ? "Brief sent" : "Written to the dry-run outbox");
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setSending(false);
    }
  }

  const title = cur
    ? `${KIND_LABEL[kind] === "Ask answers" ? "Answer" : `${KIND_LABEL[kind]} brief`} of ${fmtDateTime(cur.created)}${cur.delivered ? `, delivered via ${cur.channel}` : ", not yet delivered"}.`
    : `No ${kind === "ask" ? "saved answers" : `${kind} briefs`} yet.`;

  return (
    <>
      <PageHead
        title={title}
        deck="Briefs are written by every run (daily) and by `fieldnote brief weekly`; each statement in them is a verified claim with its source."
      />
      <div className="toolbar" style={{ marginTop: 26 }}>
        <Seg
          label="Brief type"
          value={kind}
          options={KINDS}
          format={(k) => KIND_LABEL[k]}
          onChange={(k) => {
            setKind(k);
            setSel(null);
            setConfirm(false);
            setResults(null);
          }}
        />
      </div>

      {!briefs.length ? (
        <Empty
          title={`No ${kind === "ask" ? "saved answers" : `${kind} briefs`} yet.`}
          hint={kind === "weekly" ? "Run `fieldnote brief weekly` to write the weekly decision memo." : "Briefs are written by `fieldnote run`."}
        />
      ) : (
        <div className="split" style={{ marginTop: 26 }}>
          <nav className="doc-list" aria-label="Briefs">
            {briefs.map((b) => (
              <button
                key={b.id}
                type="button"
                className="doc-item"
                aria-current={b.id === cur?.id}
                onClick={() => {
                  setSel(b.id);
                  setConfirm(false);
                  setResults(null);
                }}
              >
                <b>{kind === "ask" && b.question ? b.question : fmtDateTime(b.created)}</b>
                #{b.id}
                {b.run_id ? ` · run ${b.run_id}` : ""}
                {b.delivered ? ` · sent via ${b.channel}` : ""}
              </button>
            ))}
          </nav>
          {cur ? (
            <div style={{ minWidth: 0 }}>
              <Exhibit
                n={1}
                first
                title={kind === "ask" && cur.question ? cur.question : `Brief #${cur.id}`}
                tools={
                  <>
                    {cur.formats.map((f) => (
                      <a key={f} className="btn sm" href={`/api/ws/${ws}/briefs/${cur.id}/file/${f}`} download>
                        <Download size={14} /> {f.toUpperCase()}
                      </a>
                    ))}
                    {kind !== "ask" ? (
                      <button type="button" className="btn sm primary" onClick={() => setConfirm((c) => !c)} aria-expanded={confirm}>
                        <Send size={14} /> Send now
                      </button>
                    ) : null}
                  </>
                }
              >
                <Reveal open={confirm}>
                  <div className="inline-confirm" style={{ marginBottom: 24 }}>
                    <p>
                      This delivers the latest {kind} brief to <b>{where}</b>.
                      {!data.channels.length && data.dry_run_reasons.length ? (
                        <span className="hint"> ({data.dry_run_reasons.join("; ")})</span>
                      ) : null}
                    </p>
                    <div style={{ display: "flex", gap: 10 }}>
                      <button type="button" className="btn primary sm" onClick={send} disabled={sending}>
                        {sending ? <Spinner /> : <Send size={14} />} Confirm send
                      </button>
                      <button type="button" className="btn sm" onClick={() => setConfirm(false)}>
                        Cancel
                      </button>
                    </div>
                  </div>
                </Reveal>
                {results ? (
                  <div style={{ display: "grid", gap: 8, marginBottom: 24 }}>
                    {results.map((r) => (
                      <Note key={r.channel} tone={r.status === "sent" || r.status === "dry_run" ? "ok" : "warn"}>
                        <b>{r.channel}</b>: <Mark kind="plain">{r.status}</Mark> {r.message}
                      </Note>
                    ))}
                  </div>
                ) : null}
                <Doc ws={ws!} id={cur.id} key={cur.id} />
              </Exhibit>
            </div>
          ) : null}
        </div>
      )}
    </>
  );
}
