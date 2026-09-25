import { useCallback, useEffect, useRef, useState } from "react";

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

export async function api<T>(path: string, init?: RequestInit & { json?: unknown }): Promise<T> {
  const { json, ...rest } = init ?? {};
  const res = await fetch(`/api${path}`, {
    credentials: "same-origin",
    ...rest,
    headers: json !== undefined ? { "Content-Type": "application/json", ...(rest.headers ?? {}) } : rest.headers,
    body: json !== undefined ? JSON.stringify(json) : rest.body,
  });
  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!res.ok) {
    const msg = (data as { error?: string } | null)?.error ?? `Request failed (${res.status}).`;
    if (res.status === 401) window.dispatchEvent(new CustomEvent("fn:unauthorized"));
    throw new ApiError(msg, res.status);
  }
  return data as T;
}

const cache = new Map<string, unknown>();

export function invalidate(prefix: string) {
  for (const key of [...cache.keys()]) if (key.startsWith(prefix)) cache.delete(key);
}

/** Fetch a GET endpoint; cached per path so revisiting a page renders instantly while it revalidates. */
export function useApi<T>(path: string | null) {
  const [data, setData] = useState<T | undefined>(() => (path ? (cache.get(path) as T | undefined) : undefined));
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(!!path && !cache.has(path));
  const seq = useRef(0);

  const load = useCallback(async () => {
    if (!path) return;
    const n = ++seq.current;
    setLoading(!cache.has(path));
    try {
      const d = await api<T>(path);
      if (n !== seq.current) return;
      cache.set(path, d);
      setData(d);
      setError(null);
    } catch (e) {
      if (n !== seq.current) return;
      setError((e as Error).message);
    } finally {
      if (n === seq.current) setLoading(false);
    }
  }, [path]);

  useEffect(() => {
    setData(path ? (cache.get(path) as T | undefined) : undefined);
    setError(null);
    void load();
  }, [path, load]);

  // Live updates: re-fetch in place when the server reports new data for this workspace.
  useEffect(() => {
    if (!path) return;
    const on = (e: Event) => {
      const prefix = (e as CustomEvent<string>).detail;
      if (typeof prefix === "string" && path.startsWith(prefix)) void load();
    };
    window.addEventListener("fn:refresh", on);
    return () => window.removeEventListener("fn:refresh", on);
  }, [path, load]);

  return { data, error, loading, reload: load, setData };
}

// ---- types ---------------------------------------------------------------------------------------

export interface Meta {
  auth_required: boolean;
  authed: boolean;
  readonly: boolean;
  demo: boolean;
  library_count: number;
  workspaces?: { name: string; display_name: string; valid: boolean; error: string }[];
  default_workspace?: string;
}

export interface RunInfo {
  id: number;
  run_date: string;
  mode: string;
  kind: string;
  status: string;
  cost_usd: number;
  tokens_in: number;
  tokens_out: number;
  started_at: string | null;
  finished_at: string | null;
  as_of: string | null;
  stats?: Record<string, any>;
}

export interface WsContext {
  workspace: string;
  display_name: string;
  fixture: boolean;
  now: string;
  database: string;
  competitors: string[];
  run: RunInfo | null;
  weights: Record<Criterion, number>;
  top_n: number;
  demo: boolean;
}

export interface RunBrief {
  id: number;
  kind: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  documents_new: number | null;
  changes: number | null;
  cost_usd: number;
}

export interface LiveStatus {
  health: "live" | "collecting" | "stale" | "starting" | "waiting" | "demo";
  now: string;
  updated_at: string | null;
  scheduler: {
    enabled: boolean;
    pulse_minutes: number;
    analysis_hours: number;
    deliver_hour: number;
    in_process: boolean;
    last_tick: string | null;
    last_error: string;
  };
  last_pulse: RunBrief | null;
  last_analysis: RunBrief | null;
  next_pulse: string | null;
  next_analysis: string | null;
  active: (RunBrief & { log_line?: string }) | null;
  sources: { name: string; status: string; message: string; items: number | null }[];
  totals: { documents: number; documents_24h: number; changes_24h: number };
}

export interface LatestItem {
  key: string;
  id: number;
  type: "document" | "change";
  source_type: string;
  title: string;
  summary: string;
  source: string;
  url: string;
  at: string | null;
  fetched_at: string | null;
  entities: string[];
  kind?: string;
  significance?: number;
}

export type Criterion = "market_size" | "competitor_gap" | "evidence_strength" | "effort";
export const CRITERIA: Criterion[] = ["market_size", "competitor_gap", "evidence_strength", "effort"];
export const CRITERION_LABEL: Record<Criterion, string> = {
  market_size: "Market size",
  competitor_gap: "Competitor gap",
  evidence_strength: "Evidence strength",
  effort: "Effort (inverse)",
};

export interface Evidence {
  n: number;
  claim_id: number;
  type: string;
  label: string;
  text: string;
  quote: string;
  source_title: string;
  source_name: string;
  source_type: string;
  url: string;
  published: string;
  extra_sources: number;
  show_quote: boolean;
}

export interface Finding {
  id: number;
  title: string;
  category: string;
  status: string;
  scores: Record<Criterion, number>;
  evidence_count: number;
  first_seen: string;
  last_seen: string;
  raw: Record<string, { value?: number; rationale?: string }>;
  disputed: boolean;
  counter_evidence: string[];
  what: string;
  why: string;
  action: string;
  how: string;
  entities: string[];
  evidence: Evidence[];
  rank?: number;
  total?: number;
}

export interface Overview {
  kpis: {
    documents: number;
    documents_7d: number;
    changes_7d: number;
    changes_by_competitor: Record<string, number>;
    verified_pct: number;
    checked: number;
    open_actions: number;
    overdue_actions: number;
  };
  findings_total: number;
  top: Finding[];
}

export type DiffSeg = ["eq" | "del" | "ins", string];

export interface Change {
  id: number;
  competitor: string;
  kind: string;
  significance: number;
  summary: string;
  url: string;
  detected_at: string;
  before: DiffSeg[];
  after: DiffSeg[];
  run_id: number | null;
}

export interface EntityMetric {
  entity: string;
  latest: number;
  previous: number | null;
  pct_change: number | null;
  share: number | null;
}

export interface MetricSummary {
  name: string;
  label: string;
  unit: string;
  unit_short: string;
  source_url: string;
  periods: string[];
  latest_period: string | null;
  previous_period: string | null;
  total_latest: number;
  total_previous: number | null;
  movers: string[];
  entities: EntityMetric[];
}

export interface MetricsResp {
  configured: boolean;
  summaries: MetricSummary[];
  points: { connector: string; entity: string; region: string; period: string; value: number }[];
}

export interface VoiceResp {
  themes: {
    id: number;
    label: string;
    size: number;
    size_prev: number;
    sentiment: number;
    emerging: boolean;
    keywords: string[];
    samples: { text: string; url: string; source: string }[];
  }[];
  heat: { aspect: string; entity: string; mean: number; n: number }[];
  tags_total: number;
  cvr: {
    entity: string;
    metric: string;
    unit: string;
    claimed: number | null;
    claimed_excerpt: string;
    median: number | null;
    q1: number | null;
    q3: number | null;
    n: number;
    low: boolean;
    samples: { value: number; excerpt: string; document_id: number }[];
  }[];
  competitors: string[];
}

export interface AskAnswer {
  session: string;
  question: string;
  intent: string;
  route_via: string;
  sentences: { text: string; citations: number[] }[];
  sources: Evidence[];
  missing: string;
  markdown: string;
}

export interface BriefRow {
  id: number;
  kind: string;
  run_id: number | null;
  formats: string[];
  question: string;
  created: string;
  delivered: string | null;
  channel: string | null;
}

export interface ActionItem {
  id: number;
  description: string;
  owner: string;
  due_date: string;
  priority: string;
  status: string;
  confidence: number;
  flags: string;
  source_sentence: string;
  created_at: string;
  last_reminded_at: string;
}

export interface Trace {
  agent: string;
  step: string;
  status: string;
  tool_calls: unknown[];
  tokens_in: number;
  tokens_out: number;
  cost: number;
  latency_ms: number;
  error: string | null;
  output: string;
}

export interface ClaimRow {
  id: number;
  agent: string;
  type: string;
  status: string;
  text: string;
  critic: string;
  stage2: string;
}

export interface EvalRow {
  id: number;
  created: string;
  mode: string;
  passed: boolean;
  metrics: Record<string, any>;
}
