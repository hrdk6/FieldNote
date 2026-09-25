import { AnimatePresence, LayoutGroup, motion } from "motion/react";
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import {
  Check,
  ChevronsUpDown,
  FileText,
  FlaskConical,
  Gauge,
  LayoutList,
  ListChecks,
  Menu,
  MessageSquareText,
  MessagesSquare,
  Plus,
  ScanSearch,
  Settings2,
  Store,
  TrendingUp,
} from "lucide-react";
import type { Meta, WsContext } from "./api";
import { applyMotion, applyTheme, fmtDate, fmtDateTime, readMotion, readTheme, type MotionPref, type ThemePref } from "./util";
import { Seg } from "./components/ui";
import { ago, elapsed, until, useLive, useNow } from "./live";

export const PAGES = [
  { path: "overview", title: "Overview", icon: Gauge },
  { path: "market", title: "Market & competitors", icon: Store },
  { path: "metrics", title: "Metrics", icon: TrendingUp },
  { path: "voice", title: "Customer voice", icon: MessagesSquare },
  { path: "board", title: "Opportunity Board", icon: LayoutList },
  { path: "ask", title: "Ask FieldNote", icon: MessageSquareText },
  { path: "briefs", title: "Briefs", icon: FileText },
  { path: "actions", title: "Actions", icon: ListChecks },
  { path: "inspector", title: "Run Inspector", icon: ScanSearch },
  { path: "evals", title: "Evals", icon: FlaskConical },
] as const;

export const SETUP = { path: "workspaces", title: "Workspaces", icon: Settings2 };

interface Shell {
  meta: Meta;
  ws: string | null;
  ctx: WsContext | null;
  reloadMeta: () => void;
  reloadCtx: () => void;
}

export const ShellCtx = createContext<Shell>(null as unknown as Shell);
export const useShell = () => useContext(ShellCtx);

function Brand() {
  return (
    <a className="brand" href="/" aria-label="FieldNote home">
      <svg width="28" height="28" viewBox="0 0 32 32" aria-hidden>
        <rect width="32" height="32" rx="3" fill="var(--navy)" />
        <rect x="7" y="17" width="4" height="8" fill="var(--steel-2)" />
        <rect x="14" y="11" width="4" height="14" fill="var(--steel-2)" />
        <rect x="21" y="7" width="4" height="18" fill="var(--rust)" />
        <rect x="7" y="26.5" width="18" height="1.5" fill="var(--paper)" />
      </svg>
      <span>
        <span className="brand-name">FieldNote</span>
        <span className="brand-sub">Market intelligence, sourced</span>
      </span>
    </a>
  );
}

function WorkspaceSwitcher() {
  const { meta, ws, ctx } = useShell();
  const [open, setOpen] = useState(false);
  const nav = useNavigate();
  const loc = useLocation();
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => !box.current?.contains(e.target as Node) && setOpen(false);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  const list = (meta.workspaces ?? []).filter((w) => w.valid);
  const invalid = (meta.workspaces ?? []).filter((w) => !w.valid);
  const current = list.find((w) => w.name === ws);
  const sub = loc.pathname.split("/")[2] || "overview";
  return (
    <div className="ws-switch" ref={box}>
      <button type="button" className="ws-button" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <span style={{ minWidth: 0 }}>
          <small>Workspace</small>
          <b>{current?.display_name ?? ctx?.display_name ?? "None yet"}</b>
        </span>
        <ChevronsUpDown size={15} color="var(--ink-3)" />
      </button>
      <AnimatePresence>
        {open ? (
          <motion.div
            className="ws-menu"
            role="menu"
            initial={{ opacity: 0, y: -4, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -4, transition: { duration: 0.1 } }}
            transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
          >
            {list.map((w) => (
              <button
                key={w.name}
                type="button"
                role="menuitemradio"
                aria-checked={w.name === ws}
                onClick={() => {
                  setOpen(false);
                  nav(`/${w.name}/${sub === "workspaces" ? "overview" : sub}`);
                }}
              >
                <span style={{ width: 14, display: "flex" }}>{w.name === ws ? <Check size={14} /> : null}</span>
                {w.display_name}
              </button>
            ))}
            {invalid.length ? (
              <p className="hint" style={{ padding: "6px 9px" }}>
                Invalid config: {invalid.map((w) => w.name).join(", ")}. Fix it under Workspaces.
              </p>
            ) : null}
            <hr />
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                nav(`/${ws ?? "_"}/workspaces?tab=new`);
              }}
            >
              <span style={{ width: 14, display: "flex" }}>
                <Plus size={14} />
              </span>
              Add from the library ({meta.library_count} topics)
            </button>
          </motion.div>
        ) : null}
      </AnimatePresence>
    </div>
  );
}

function ThemeToggle() {
  const [t, setT] = useState<ThemePref>(readTheme());
  return (
    <div className="row">
      <span>Theme</span>
      <Seg<ThemePref>
        label="Colour theme"
        value={t}
        options={["light", "dark", "system"] as const}
        format={(v) => (v === "system" ? "Auto" : v === "light" ? "Light" : "Dark")}
        onChange={(v) => {
          setT(v);
          applyTheme(v);
        }}
      />
    </div>
  );
}

function MotionToggle() {
  const [m, setM] = useState<MotionPref>(readMotion());
  return (
    <div className="row">
      <span>Motion</span>
      <Seg<MotionPref>
        label="Motion"
        value={m}
        options={["full", "reduced", "system"] as const}
        format={(v) => (v === "system" ? "Auto" : v === "full" ? "Full" : "Less")}
        onChange={(v) => {
          setM(v);
          applyMotion(v);
        }}
      />
    </div>
  );
}

export function Sidebar({ open, onNavigate }: { open: boolean; onNavigate: () => void }) {
  const { ws, ctx, meta } = useShell();
  const loc = useLocation();
  const base = ws ? `/${ws}` : "/_";
  return (
    <aside className={`sidebar${open ? " open" : ""}`} aria-label="Contents">
      <Brand />
      <WorkspaceSwitcher />
      <LayoutGroup id="nav">
        <nav className="nav" aria-label="Pages">
          {ws && ctx ? (
            <>
              <div className="nav-group">Insights</div>
              {PAGES.map((p) => (
                <NavItem key={p.path} to={`${base}/${p.path}`} icon={<p.icon size={16} strokeWidth={1.8} />} onClick={onNavigate}>
                  {p.title}
                </NavItem>
              ))}
            </>
          ) : null}
          <div className="nav-group">Setup</div>
          <NavItem to={`${base}/workspaces`} icon={<SETUP.icon size={16} strokeWidth={1.8} />} onClick={onNavigate}>
            {SETUP.title}
          </NavItem>
        </nav>
      </LayoutGroup>
      <div className="sidebar-foot">
        {ctx ? (
          <div className="row">
            <span>Database</span>
            <code>{ctx.database}</code>
          </div>
        ) : null}
        {meta.readonly ? (
          <div className="row">
            <span>Mode</span>
            <span className="mark plain">Read-only</span>
          </div>
        ) : null}
        <ThemeToggle />
        <MotionToggle />
        <span className="sr-only">{loc.pathname}</span>
      </div>
    </aside>
  );
}

function NavItem({ to, icon, children, onClick }: { to: string; icon: ReactNode; children: ReactNode; onClick: () => void }) {
  return (
    <NavLink to={to} className={({ isActive }) => `nav-item${isActive ? " active" : ""}`} onClick={onClick}>
      {({ isActive }) => (
        <>
          {isActive ? (
            <motion.span
              layoutId="nav-highlight"
              className="nav-highlight"
              transition={{ type: "spring", stiffness: 560, damping: 44 }}
            />
          ) : null}
          {icon}
          {children}
        </>
      )}
    </NavLink>
  );
}

export function FixtureBand() {
  return (
    <div className="fixture-band" role="note">
      <FlaskConical size={15} style={{ flex: "none", marginTop: 2 }} />
      <span>
        <b>FIXTURE DATA.</b> This workspace is showing the bundled synthetic demo dataset (fictional brands,{" "}
        <code>.example</code> links). It is not real market information.
      </span>
    </div>
  );
}

export function RunHead({ page }: { page: string }) {
  const { ctx, meta } = useShell();
  const { status, active, connected } = useLive();
  const now = useNow(active ? 1000 : 20000);
  let dot = "idle";
  let label: ReactNode = null;
  let detail: ReactNode = null;
  if (meta.demo || ctx?.fixture) {
    dot = "idle";
    label = "Demo data";
    detail = "Bundled fixture dataset, not live";
  } else if (active) {
    dot = "busy";
    label = `${active.kind === "pulse" ? "Collecting sources" : "Running full analysis"} · ${elapsed(active.started_at, now)}`;
    detail = active.log_line ? <span className="runhead-log">{active.log_line}</span> : null;
  } else if (status) {
    if (status.health === "live") {
      dot = "live";
      label = `Live · updated ${ago(status.updated_at, now)}`;
      detail = status.next_pulse ? `Next check ${until(status.next_pulse, now)}` : null;
    } else if (status.health === "stale") {
      dot = "stale";
      label = `Last updated ${ago(status.updated_at, now)}`;
      detail = status.scheduler.enabled ? `Next check ${until(status.next_pulse, now)}` : "Live collection is off (FIELDNOTE_SCHEDULER)";
    } else if (status.health === "starting") {
      dot = "busy";
      label = "Starting the first live collection";
    } else {
      dot = "stale";
      label = "No live data yet";
      detail = "Start a collection under Workspaces";
    }
  }
  return (
    <div className="runhead">
      <span className="runhead-page">{page}</span>
      <span className="runhead-meta" aria-live="polite">
        {label ? (
          <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
            <i className={`dot ${dot}`} aria-hidden />
            {label}
          </span>
        ) : null}
        {detail ? <span className="hide-sm">{detail}</span> : null}
        {!meta.demo && ctx ? (
          <span className="hide-sm" title={connected ? "Receiving live updates" : "Reconnecting to live updates"}>
            {connected ? "Stream on" : "Reconnecting…"}
          </span>
        ) : null}
      </span>
    </div>
  );
}

export function MobileBar({ onMenu }: { onMenu: () => void }) {
  const { ctx } = useShell();
  return (
    <div className="mobile-bar">
      <button type="button" className="btn sm" onClick={onMenu} aria-label="Open contents">
        <Menu size={16} /> Contents
      </button>
      <span style={{ fontWeight: 650, color: "var(--ink-strong)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {ctx?.display_name ?? "FieldNote"}
      </span>
    </div>
  );
}

export function runLine(ctx: WsContext | null): string {
  const r = ctx?.run;
  if (!r) return "No analysis has finished yet for this workspace.";
  const when = r.finished_at ? `${fmtDateTime(r.finished_at)} UTC` : fmtDate(r.run_date);
  return `Findings from analysis #${r.id} (${when}, ${r.mode === "offline" ? "fixture data" : "live sources"}, ${r.status}).`;
}

