import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import { ArrowUp, RotateCcw } from "lucide-react";
import { api, useApi, type AskAnswer } from "../api";
import { EASE, EvidenceList, Note, PageHead, Sk, Spinner, useToast } from "../components/ui";
import { useShell } from "../shell";

const EXAMPLES = [
  "What changed in competitor pricing this week?",
  "What are customers complaining about?",
  "Which brands gained share last month?",
  "What are our top opportunities?",
  "Which action items are open?",
];

type Turn = { q: string; a?: AskAnswer; error?: string };
const THREADS = new Map<string, { session: string | null; turns: Turn[] }>();

function Answer({ a, idx }: { a: AskAnswer; idx: number }) {
  const prefix = `q${idx}-src`;
  return (
    <>
      {a.sentences.length ? (
        <p className="qa-a">
          {a.sentences.map((s, i) => (
            <span key={i} className="sent">
              {s.text}
              {s.citations.length ? (
                <sup>
                  {s.citations.map((c) => (
                    <a key={c} href={`#${prefix}-${c}`} aria-label={`Source ${c}`}>
                      [{c}]
                    </a>
                  ))}
                </sup>
              ) : null}
            </span>
          ))}
        </p>
      ) : null}
      {a.missing ? (
        <div className="qa-missing">
          <Note tone="warn">
            <b>Not enough evidence.</b> {a.missing}
          </Note>
        </div>
      ) : null}
      {a.sources.length ? (
        <div style={{ marginTop: 18, maxWidth: "86ch" }}>
          <div className="ex-source" style={{ marginTop: 0 }}>
            <b>Sources</b>
          </div>
          <EvidenceList items={a.sources} idPrefix={prefix} />
        </div>
      ) : null}
      <p className="qa-meta">
        Intent: {a.intent.replace(/_/g, " ")} · routed via {a.route_via}
      </p>
    </>
  );
}

export default function Ask() {
  const { ws, ctx } = useShell();
  const info = useApi<{ llm: string }>(`/ws/${ws}/ask`);
  const toast = useToast();
  const key = ws ?? "";
  const [thread, setThread] = useState(() => THREADS.get(key) ?? { session: null, turns: [] as Turn[] });
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    THREADS.set(key, thread);
  }, [key, thread]);

  useEffect(() => {
    const el = input.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 180)}px`;
  }, [q]);

  async function send(question: string) {
    const text = question.trim();
    if (!text || busy) return;
    setQ("");
    setBusy(true);
    const idx = thread.turns.length;
    setThread((t) => ({ ...t, turns: [...t.turns, { q: text }] }));
    window.setTimeout(() => bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" }), 40);
    try {
      const a = await api<AskAnswer>(`/ws/${ws}/ask`, { method: "POST", json: { question: text, session: thread.session } });
      setThread((t) => ({ session: a.session, turns: t.turns.map((x, i) => (i === idx ? { ...x, a } : x)) }));
    } catch (e) {
      setThread((t) => ({ ...t, turns: t.turns.map((x, i) => (i === idx ? { ...x, error: (e as Error).message } : x)) }));
    } finally {
      setBusy(false);
    }
  }

  async function reset() {
    if (thread.session) await api(`/ws/${ws}/ask/reset`, { method: "POST", json: { session: thread.session } }).catch(() => {});
    setThread({ session: null, turns: [] });
    toast("Started a new conversation");
  }

  return (
    <>
      <PageHead
        title={thread.turns.length ? thread.turns[thread.turns.length - 1].q : `Ask anything about ${ctx?.display_name ?? "this workspace"}.`}
        deck={
          <>
            Answers use only the local database and cite their sources; if the evidence is not there, FieldNote says what is missing.
            {info.data ? ` Model: ${info.data.llm}.` : ""}
          </>
        }
      >
        {thread.turns.length ? (
          <button type="button" className="btn sm" onClick={reset} disabled={busy}>
            <RotateCcw size={14} /> New conversation
          </button>
        ) : null}
      </PageHead>

      {!thread.turns.length ? (
        <div className="exhibit first">
          <div className="ex-head">
            <span className="ex-title">Start with a question the database can answer</span>
          </div>
          <div className="chips">
            {EXAMPLES.map((e) => (
              <button key={e} type="button" className="chip" onClick={() => send(e)}>
                {e}
              </button>
            ))}
          </div>
        </div>
      ) : null}

      <div className="thread" style={{ marginTop: thread.turns.length ? 28 : 0, borderTop: thread.turns.length ? "2px solid var(--rule-strong)" : undefined }}>
        <AnimatePresence initial={false}>
          {thread.turns.map((t, i) => (
            <motion.section
              key={i}
              className="qa"
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.28, ease: EASE }}
            >
              <h2 className="qa-q">
                <span className="qn">Q{i + 1}</span>
                <span>{t.q}</span>
              </h2>
              {t.a ? (
                <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: 0.3 }}>
                  <Answer a={t.a} idx={i} />
                </motion.div>
              ) : t.error ? (
                <div style={{ marginTop: 14 }}>
                  <Note tone="warn">{t.error}</Note>
                </div>
              ) : (
                <div style={{ marginTop: 16, maxWidth: "76ch" }} aria-live="polite">
                  <p className="hint" style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 12 }}>
                    <Spinner /> Searching the local database and checking citations…
                  </p>
                  <Sk w="96%" />
                  <Sk w="88%" style={{ marginTop: 9 }} />
                  <Sk w="64%" style={{ marginTop: 9 }} />
                </div>
              )}
            </motion.section>
          ))}
        </AnimatePresence>
      </div>
      <div ref={bottom} />

      <div className="composer">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void send(q);
          }}
        >
          <label htmlFor="ask-q" className="sr-only">
            Question
          </label>
          <textarea
            id="ask-q"
            ref={input}
            rows={1}
            value={q}
            maxLength={2000}
            placeholder="Ask about changes, competitors, customers, metrics, opportunities or actions"
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void send(q);
              }
            }}
          />
          <button className="btn primary" disabled={busy || !q.trim()} aria-label="Ask">
            {busy ? <Spinner /> : <ArrowUp size={16} />} Ask
          </button>
        </form>
      </div>
    </>
  );
}
