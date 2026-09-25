/* Live connection: one Server-Sent Events stream per workspace. When the server reports new data, every
   mounted query for that workspace re-fetches in place (no skeletons), and the viewer gets a short note. */

import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { api, invalidate, type LiveStatus } from "./api";
import { useToast } from "./components/ui";
import { plural } from "./util";

interface ActiveRun {
  id: number;
  kind: string;
  started_at: string | null;
  log_line: string;
}

interface Live {
  status: LiveStatus | null;
  active: ActiveRun | null;
  connected: boolean;
  reload: () => Promise<void>;
}

const LiveCtx = createContext<Live>({ status: null, active: null, connected: false, reload: async () => {} });
export const useLive = () => useContext(LiveCtx);

/** Tell every mounted query under this prefix to re-fetch silently. */
export function refreshQueries(prefix: string) {
  invalidate(prefix);
  window.dispatchEvent(new CustomEvent("fn:refresh", { detail: prefix }));
}

export function LiveProvider({ ws, children }: { ws: string | null; children: ReactNode }) {
  const toast = useToast();
  const [status, setStatus] = useState<LiveStatus | null>(null);
  const [active, setActive] = useState<ActiveRun | null>(null);
  const [connected, setConnected] = useState(false);
  const version = useRef<string | null>(null);
  const docs = useRef<number | null>(null);

  const reload = useCallback(async () => {
    if (!ws) return;
    try {
      const s = await api<LiveStatus>(`/ws/${ws}/live`);
      setStatus(s);
      setActive(s.active ? { id: s.active.id, kind: s.active.kind, started_at: s.active.started_at, log_line: s.active.log_line ?? "" } : null);
    } catch {
      /* the header simply keeps its last known state */
    }
  }, [ws]);

  useEffect(() => {
    setStatus(null);
    setActive(null);
    version.current = null;
    docs.current = null;
    if (!ws) return;
    void reload();
    if (typeof EventSource === "undefined") {
      const t = window.setInterval(() => void reload(), 30000);
      return () => window.clearInterval(t);
    }
    const es = new EventSource(`/api/ws/${ws}/events`);
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    es.addEventListener("state", (ev) => {
      setConnected(true);
      let data: { version: string; documents: number; changes: number; active: ActiveRun | null };
      try {
        data = JSON.parse((ev as MessageEvent).data);
      } catch {
        return;
      }
      setActive(data.active);
      if (version.current !== null && data.version !== version.current) {
        refreshQueries(`/ws/${ws}/`);
        void api<LiveStatus>(`/ws/${ws}/live`)
          .then((s) => {
            setStatus(s);
            const before = docs.current;
            docs.current = s.totals.documents;
            const added = before === null ? 0 : s.totals.documents - before;
            if (added > 0) toast(`Live update: ${plural(added, "new item")} collected`);
            else if (!data.active) toast("Live data refreshed");
          })
          .catch(() => {});
      } else if (version.current === null) {
        void reload().then(() => {
          docs.current = null;
        });
      }
      version.current = data.version;
    });
    return () => {
      es.close();
      setConnected(false);
    };
  }, [ws, reload, toast]);

  useEffect(() => {
    if (status && docs.current === null) docs.current = status.totals.documents;
  }, [status]);

  return <LiveCtx.Provider value={{ status, active, connected, reload }}>{children}</LiveCtx.Provider>;
}

/** Re-render every `ms` so relative times ("4 min ago") stay true. */
export function useNow(ms = 30000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(t);
  }, [ms]);
  return now;
}

/** Server timestamps are naive UTC ("2026-09-25T08:10:00"). */
export function utcMs(s: string | null | undefined): number | null {
  if (!s) return null;
  const d = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(s) ? s : `${s}Z`);
  return Number.isNaN(d.getTime()) ? null : d.getTime();
}

export function ago(s: string | null | undefined, now: number): string {
  const t = utcMs(s);
  if (t === null) return "";
  const sec = Math.round((now - t) / 1000);
  if (sec < 45) return "just now";
  const min = Math.round(sec / 60);
  if (min < 60) return `${min} min ago`;
  const h = Math.round(min / 60);
  if (h < 36) return `${h} h ago`;
  const d = Math.round(h / 24);
  return `${d} day${d === 1 ? "" : "s"} ago`;
}

export function until(s: string | null | undefined, now: number): string {
  const t = utcMs(s);
  if (t === null) return "";
  const min = Math.round((t - now) / 60000);
  if (min <= 0) return "due now";
  if (min < 60) return `in ${min} min`;
  const h = Math.round(min / 60);
  return `in ${h} h`;
}

export function elapsed(s: string | null | undefined, now: number): string {
  const t = utcMs(s);
  if (t === null) return "";
  const sec = Math.max(0, Math.round((now - t) / 1000));
  const m = Math.floor(sec / 60);
  return `${m}:${String(sec % 60).padStart(2, "0")}`;
}
