import { AnimatePresence, animate, motion, useInView } from "motion/react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { AlertTriangle, CheckCircle2, ChevronRight, CircleSlash, Clock3, FlaskConical, Info, ShieldCheck, X } from "lucide-react";
import type { Evidence } from "../api";
import { fmtDate, useReducedMotion } from "../util";

export const EASE = [0.16, 1, 0.3, 1] as const;

// ---- page and exhibit ----------------------------------------------------------------------------

export function PageHead({ title, deck, children }: { title: ReactNode; deck?: ReactNode; children?: ReactNode }) {
  return (
    <header>
      <h1 className="page-title">{title}</h1>
      {deck ? <p className="page-deck">{deck}</p> : null}
      {children ? <div className="page-actions">{children}</div> : null}
    </header>
  );
}

export function Exhibit({
  n,
  title,
  tools,
  source,
  note,
  first,
  children,
  id,
}: {
  n: number | string;
  title: ReactNode;
  tools?: ReactNode;
  source?: ReactNode;
  note?: ReactNode;
  first?: boolean;
  children: ReactNode;
  id?: string;
}) {
  return (
    <figure className={`exhibit${first ? " first" : ""}`} id={id} style={{ marginInline: 0 }}>
      <figcaption className="ex-head">
        <span className="ex-title">
          <span className="ex-num">Exhibit {n}</span> {title}
        </span>
        {tools ? <span className="ex-tools">{tools}</span> : null}
      </figcaption>
      {children}
      {source || note ? (
        <footer className="ex-source">
          {source ? (
            <p>
              <b>Source:</b> {source}
            </p>
          ) : null}
          {note ? (
            <p>
              <b>Note:</b> {note}
            </p>
          ) : null}
        </footer>
      ) : null}
    </figure>
  );
}

// ---- figures -------------------------------------------------------------------------------------

export function CountUp({ value, format }: { value: number; format: (n: number) => string }) {
  const ref = useRef<HTMLSpanElement>(null);
  const reduced = useReducedMotion();
  const last = useRef<number | null>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const from = last.current ?? (reduced ? value : value * 0.6);
    last.current = value;
    if (reduced || from === value) {
      el.textContent = format(value);
      return;
    }
    const c = animate(from, value, { duration: 0.55, ease: EASE, onUpdate: (v) => (el.textContent = format(v)) });
    return () => c.stop();
  }, [value, format, reduced]);
  return <span ref={ref}>{format(value)}</span>;
}

export function Figures({ children }: { children: ReactNode }) {
  return <div className="figures">{children}</div>;
}

export function Figure({
  label,
  value,
  sub,
  fn,
  tone,
}: {
  label: ReactNode;
  value: ReactNode;
  sub?: ReactNode;
  fn?: number;
  tone?: "warn" | "bad";
}) {
  return (
    <div className="figure">
      <div className="f-label">{label}</div>
      <div className={`f-value${tone ? ` ${tone}` : ""}`}>
        {value}
        {fn ? <sup className="fn">{fn}</sup> : null}
      </div>
      {sub ? <div className="f-sub">{sub}</div> : null}
    </div>
  );
}

// ---- marks ---------------------------------------------------------------------------------------

type MarkKind = "verified" | "thin" | "disputed" | "stale" | "fixture" | "plain" | "";

export function Mark({ kind = "", children, title }: { kind?: MarkKind; children: ReactNode; title?: string }) {
  const icon =
    kind === "verified" ? (
      <ShieldCheck size={12} strokeWidth={2.2} />
    ) : kind === "thin" ? (
      <AlertTriangle size={12} strokeWidth={2.2} />
    ) : kind === "disputed" ? (
      <CircleSlash size={12} strokeWidth={2.2} />
    ) : kind === "stale" ? (
      <Clock3 size={12} strokeWidth={2.2} />
    ) : kind === "fixture" ? (
      <FlaskConical size={12} strokeWidth={2.2} />
    ) : null;
  return (
    <span className={`mark ${kind}`} title={title}>
      {icon}
      {children}
    </span>
  );
}

export function StatusMarks({ status, disputed }: { status: string; disputed?: boolean }) {
  return (
    <>
      {status === "demoted" ? (
        <Mark kind="thin" title="Demoted: fewer independent sources than the workspace requires">
          Thin evidence
        </Mark>
      ) : null}
      {status === "stale" ? <Mark kind="stale">Stale</Mark> : null}
      {disputed ? (
        <Mark kind="disputed" title="Sources disagree; read the counter-evidence">
          Disputed
        </Mark>
      ) : null}
    </>
  );
}

// ---- segmented control ---------------------------------------------------------------------------

export function Seg<T extends string>({
  value,
  options,
  onChange,
  label,
  format,
}: {
  value: T;
  options: readonly T[];
  onChange: (v: T) => void;
  label: string;
  format?: (v: T) => string;
}) {
  const id = useId();
  return (
    <div className="seg" role="group" aria-label={label}>
      {options.map((o) => (
        <button key={o} type="button" aria-pressed={o === value} onClick={() => onChange(o)}>
          {o === value ? (
            <motion.span
              layoutId={`seg-${id}`}
              className="seg-thumb"
              transition={{ type: "spring", stiffness: 520, damping: 42 }}
            />
          ) : null}
          {format ? format(o) : o}
        </button>
      ))}
    </div>
  );
}

// ---- disclosure ----------------------------------------------------------------------------------

export function Reveal({ open, children }: { open: boolean; children: ReactNode }) {
  const reduced = useReducedMotion();
  return (
    <AnimatePresence initial={false}>
      {open ? (
        <motion.div
          className="reveal"
          initial={{ height: 0, opacity: 0 }}
          animate={{ height: "auto", opacity: 1 }}
          exit={{ height: 0, opacity: 0 }}
          transition={{ duration: reduced ? 0 : 0.26, ease: EASE }}
        >
          {children}
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
}

export function Disclosure({
  label,
  children,
  defaultOpen = false,
}: {
  label: ReactNode;
  children: ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div>
      <button type="button" className="disclose" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <ChevronRight size={15} strokeWidth={2.2} />
        {label}
      </button>
      <Reveal open={open}>{children}</Reveal>
    </div>
  );
}

// ---- evidence ------------------------------------------------------------------------------------

export function EvidenceList({ items, idPrefix }: { items: Evidence[]; idPrefix?: string }) {
  if (!items.length) return <p className="hint">No verified evidence attached.</p>;
  return (
    <ol className="evidence">
      {items.map((e) => (
        <li key={`${e.claim_id}-${e.n}`} id={idPrefix ? `${idPrefix}-${e.n}` : undefined}>
          <span className="n">[{e.n}]</span>
          <div>
            <div className="claim">
              <Mark kind={e.type === "fact" ? "verified" : "plain"}>{e.label}</Mark> {e.text}
            </div>
            {e.show_quote ? <blockquote className="quote">“{e.quote}”</blockquote> : null}
            <div className="src">
              {e.url ? (
                <a href={e.url} target="_blank" rel="noreferrer">
                  {e.source_name || e.source_title || "source"}
                </a>
              ) : (
                e.source_name
              )}
              {e.published ? ` · ${fmtDate(e.published)}` : ""}
              {e.source_type ? ` · ${e.source_type}` : ""}
              {e.extra_sources ? ` · +${e.extra_sources} more sources` : ""}
            </div>
          </div>
        </li>
      ))}
    </ol>
  );
}

// ---- states --------------------------------------------------------------------------------------

export function Empty({ title, hint, children }: { title: string; hint?: ReactNode; children?: ReactNode }) {
  return (
    <div className="empty">
      <h3>{title}</h3>
      {hint ? <p>{hint}</p> : null}
      {children ? <div style={{ marginTop: 14 }}>{children}</div> : null}
    </div>
  );
}

export function ErrorNote({ children }: { children: ReactNode }) {
  return (
    <div className="error-note" role="alert">
      <AlertTriangle size={16} style={{ flex: "none", marginTop: 2 }} />
      <div>{children}</div>
    </div>
  );
}

export function Note({ tone = "info", children }: { tone?: "info" | "warn" | "ok"; children: ReactNode }) {
  const Icon = tone === "ok" ? CheckCircle2 : tone === "warn" ? AlertTriangle : Info;
  return (
    <div className={`${tone}-note`}>
      <Icon size={16} style={{ flex: "none", marginTop: 2 }} />
      <div>{children}</div>
    </div>
  );
}

export function Sk({ w = "100%", h = 14, style }: { w?: number | string; h?: number; style?: React.CSSProperties }) {
  return <span className="sk" style={{ width: w, height: h, ...style }} aria-hidden />;
}

export function PageSkeleton({ figures = true }: { figures?: boolean }) {
  return (
    <div aria-busy="true" aria-label="Loading">
      <Sk w="62%" h={30} />
      <Sk w="44%" h={30} style={{ marginTop: 10 }} />
      <Sk w="52%" h={14} style={{ marginTop: 18 }} />
      <div className="exhibit first">
        <Sk w="36%" h={16} style={{ marginBottom: 22 }} />
        {figures ? (
          <div className="figures">
            {[0, 1, 2, 3, 4].map((i) => (
              <div className="figure" key={i}>
                <Sk w="70%" h={12} />
                <Sk w="50%" h={28} style={{ marginTop: 10 }} />
              </div>
            ))}
          </div>
        ) : (
          <Sk h={220} />
        )}
      </div>
      <div className="exhibit">
        <Sk w="30%" h={16} style={{ marginBottom: 22 }} />
        {[0, 1, 2].map((i) => (
          <div key={i} style={{ padding: "18px 0", borderBottom: "1px solid var(--rule-soft)" }}>
            <Sk w="58%" h={18} />
            <Sk w="90%" h={12} style={{ marginTop: 12 }} />
            <Sk w="80%" h={12} style={{ marginTop: 8 }} />
          </div>
        ))}
      </div>
    </div>
  );
}

// ---- in-view once (charts draw in the first time they are seen) -----------------------------------

/** Charts draw in the first time they are seen. If the viewer never scrolls to one (or the observer or clock
 *  stalls), it is shown at rest after a short grace period, so an exhibit is never left empty. */
export function useDrawIn<T extends Element>() {
  const ref = useRef<T>(null);
  const inView = useInView(ref, { once: true, margin: "0px 0px -40px 0px" });
  const reduced = useReducedMotion();
  const [late, setLate] = useState(false);
  useEffect(() => {
    if (inView) return;
    const t = window.setTimeout(() => setLate(true), 1200);
    return () => window.clearTimeout(t);
  }, [inView]);
  const settled = late && !inView;
  return { ref, shown: inView || reduced || late, instant: reduced || settled };
}

// ---- toasts --------------------------------------------------------------------------------------

type Toast = { id: number; text: string; tone: "ok" | "error" };
const ToastCtx = createContext<(text: string, tone?: "ok" | "error") => void>(() => {});

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const push = useCallback((text: string, tone: "ok" | "error" = "ok") => {
    const id = Date.now() + Math.random();
    setItems((xs) => [...xs.slice(-2), { id, text, tone }]);
    window.setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== id)), tone === "error" ? 6000 : 3200);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        <AnimatePresence>
          {items.map((t) => (
            <motion.div
              key={t.id}
              layout
              className={`toast${t.tone === "error" ? " error" : ""}`}
              initial={{ opacity: 0, y: 14, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 6, transition: { duration: 0.14 } }}
              transition={{ duration: 0.28, ease: EASE }}
            >
              {t.tone === "error" ? <AlertTriangle size={15} /> : <CheckCircle2 size={15} />}
              <span>{t.text}</span>
              <button
                type="button"
                aria-label="Dismiss"
                onClick={() => setItems((xs) => xs.filter((x) => x.id !== t.id))}
                style={{ border: 0, background: "none", color: "inherit", padding: 0, marginLeft: 4, display: "flex" }}
              >
                <X size={14} />
              </button>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </ToastCtx.Provider>
  );
}

export const useToast = () => useContext(ToastCtx);

export function Spinner() {
  return <span className="spinner" aria-hidden />;
}
