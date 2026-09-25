import { useEffect, useState } from "react";
import { CRITERIA, type Criterion, type Finding } from "./api";

// ---- formatting ----------------------------------------------------------------------------------

const nf = new Intl.NumberFormat("en-IN");
export const fmtInt = (n: number) => nf.format(Math.round(n));
export const fmtNum = (n: number, d = 1) =>
  new Intl.NumberFormat("en-IN", { maximumFractionDigits: d, minimumFractionDigits: 0 }).format(n);
export const fmtPct = (n: number, d = 0) => `${n.toFixed(d)}%`;
export const fmtSigned = (n: number, d = 1) => `${n > 0 ? "+" : n < 0 ? "−" : ""}${Math.abs(n).toFixed(d)}`;
export const fmtUsd = (n: number, d = 2) => `$${n.toFixed(d)}`;

export function compact(n: number): string {
  const a = Math.abs(n);
  if (a >= 1e7) return `${fmtNum(n / 1e7, 1)} cr`;
  if (a >= 1e5) return `${fmtNum(n / 1e5, 1)} lakh`;
  if (a >= 1e3) return `${fmtNum(n / 1e3, 1)}k`;
  return fmtNum(n, 1);
}

export function parseDate(s: string | null | undefined): Date | null {
  if (!s) return null;
  const d = new Date(s.length <= 10 ? `${s}T00:00:00` : s);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function fmtDate(s: string | null | undefined, withYear = true): string {
  const d = parseDate(s);
  if (!d) return "";
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", ...(withYear ? { year: "numeric" } : {}) });
}

export function fmtDateTime(s: string | null | undefined): string {
  const d = parseDate(s);
  if (!d) return "";
  return `${fmtDate(s)}, ${d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}`;
}

export function daysBetween(a: string, b: string): number {
  const da = parseDate(a);
  const db = parseDate(b);
  if (!da || !db) return 0;
  return Math.round((db.getTime() - da.getTime()) / 86400000);
}

export function plural(n: number, one: string, many = `${one}s`) {
  return `${fmtInt(n)} ${n === 1 ? one : many}`;
}

export const cap = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);

/** "battery_life" -> "Battery life" */
export const humanize = (s: string) => cap(s.replace(/_/g, " "));

/** "2026-08" -> "Aug 2026" (other strings pass through). */
export function fmtPeriod(p: string | null | undefined): string {
  if (!p) return "";
  const m = /^(\d{4})-(\d{2})$/.exec(p);
  if (!m) return p;
  return new Date(Number(m[1]), Number(m[2]) - 1, 1).toLocaleDateString("en-GB", { month: "short", year: "numeric" });
}

// ---- entity colours: fixed by config order, never by rank ----------------------------------------

const SLOTS = ["--c1", "--c2", "--c3", "--c4", "--c5", "--c6", "--c7", "--c8"];

export function entityColor(entities: string[], name: string): string {
  const i = entities.indexOf(name);
  if (i < 0 || i >= SLOTS.length) return "var(--context)";
  return `var(${SLOTS[i]})`;
}

/** Entities from the config first (stable colours), then any extras seen in the data. */
export function entityOrder(config: string[], seen: Iterable<string>): string[] {
  const out = [...config];
  for (const e of seen) if (!out.includes(e)) out.push(e);
  return out;
}

export const CRIT_COLOR: Record<Criterion, string> = {
  market_size: "var(--crit-1)",
  competitor_gap: "var(--crit-2)",
  evidence_strength: "var(--crit-3)",
  effort: "var(--crit-4)",
};

// ---- ranking: a port of fieldnote.scoring.opportunities.rank (same order, same ties) --------------

export function weightedTotal(scores: Record<Criterion, number>, weights: Record<Criterion, number>): number {
  const wsum = CRITERIA.reduce((a, c) => a + Math.max(0, weights[c] ?? 0), 0) || 1;
  const t = CRITERIA.reduce((a, c) => a + Math.max(0, weights[c] ?? 0) * (scores[c] ?? 0), 0) / wsum;
  return Math.round(t * 1e6) / 1e6;
}

export function contributions(scores: Record<Criterion, number>, weights: Record<Criterion, number>) {
  const wsum = CRITERIA.reduce((a, c) => a + Math.max(0, weights[c] ?? 0), 0) || 1;
  return CRITERIA.map((c) => ({ c, v: (Math.max(0, weights[c] ?? 0) * (scores[c] ?? 0)) / wsum }));
}

export function rank(items: Finding[], weights: Record<Criterion, number>): (Finding & { rank: number; total: number })[] {
  const out = items.map((it) => ({ ...it, total: weightedTotal(it.scores, weights), rank: 0 }));
  out.sort((a, b) => {
    const t = Math.round(b.total * 1e6) - Math.round(a.total * 1e6);
    if (t) return t;
    if (b.evidence_count !== a.evidence_count) return b.evidence_count - a.evidence_count;
    const ta = parseDate(a.last_seen)?.getTime() ?? 0;
    const tb = parseDate(b.last_seen)?.getTime() ?? 0;
    if (tb !== ta) return tb - ta;
    const ti = a.title.toLowerCase().localeCompare(b.title.toLowerCase());
    if (ti) return ti;
    return a.id - b.id;
  });
  out.forEach((x, i) => (x.rank = i + 1));
  return out;
}

// ---- preferences ---------------------------------------------------------------------------------

export type ThemePref = "light" | "dark" | "system";

export function readTheme(): ThemePref {
  try {
    const t = localStorage.getItem("fn-theme");
    return t === "light" || t === "dark" ? t : "system";
  } catch {
    return "system";
  }
}

export function applyTheme(t: ThemePref) {
  const root = document.documentElement;
  if (t === "system") delete root.dataset.theme;
  else root.dataset.theme = t;
  try {
    if (t === "system") localStorage.removeItem("fn-theme");
    else localStorage.setItem("fn-theme", t);
  } catch {
    /* storage unavailable: the choice lasts for this page only */
  }
  window.dispatchEvent(new CustomEvent("fn:theme"));
}

export type MotionPref = "full" | "reduced" | "system";

export function readMotion(): MotionPref {
  try {
    const t = localStorage.getItem("fn-motion");
    return t === "full" || t === "reduced" ? t : "system";
  } catch {
    return "system";
  }
}

export function applyMotion(m: MotionPref) {
  try {
    if (m === "system") localStorage.removeItem("fn-motion");
    else localStorage.setItem("fn-motion", m);
  } catch {
    /* storage unavailable: the choice lasts for this page only */
  }
  (window as unknown as { __fnMotion?: MotionPref }).__fnMotion = m;
  if (m === "system") delete document.documentElement.dataset.motion;
  else document.documentElement.dataset.motion = m;
  window.dispatchEvent(new CustomEvent("fn:motion"));
}

function currentMotion(): MotionPref {
  return (window as unknown as { __fnMotion?: MotionPref }).__fnMotion ?? readMotion();
}

/** True when the viewer asked for less motion, in the OS or in FieldNote's own setting. */
export function useReducedMotion(): boolean {
  const media = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
  const calc = () => {
    const p = currentMotion();
    return p === "reduced" || (p === "system" && media());
  };
  const [r, setR] = useState(calc);
  useEffect(() => {
    const m = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    const on = () => setR(calc());
    m?.addEventListener("change", on);
    window.addEventListener("fn:motion", on);
    return () => {
      m?.removeEventListener("change", on);
      window.removeEventListener("fn:motion", on);
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  return r;
}

/** Width of an element, tracked with ResizeObserver (charts lay out to it). */
export function useWidth<T extends HTMLElement>(): [(el: T | null) => void, number] {
  const [el, setEl] = useState<T | null>(null);
  const [w, setW] = useState(0);
  useEffect(() => {
    if (!el) return;
    const ro = new ResizeObserver((entries) => setW(Math.floor(entries[0].contentRect.width)));
    ro.observe(el);
    setW(Math.floor(el.getBoundingClientRect().width));
    return () => ro.disconnect();
  }, [el]);
  return [setEl, w];
}
