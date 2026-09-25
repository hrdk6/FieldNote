import { useMemo, useState } from "react";
import { useApi, type ClaimRow, type RunInfo, type Trace } from "../api";
import { Disclosure, Empty, ErrorNote, Exhibit, Figure, Figures, Mark, PageHead, PageSkeleton, Seg, Sk } from "../components/ui";
import { useShell } from "../shell";
import { fmtDateTime, fmtInt, plural } from "../util";

function Table({ rows, cols }: { rows: Record<string, any>[]; cols?: string[] }) {
  if (!rows.length) return <p className="hint">Nothing recorded.</p>;
  const keys = cols ?? Object.keys(rows[0]);
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            {keys.map((k) => (
              <th key={k} className={typeof rows[0][k] === "number" ? "r" : undefined}>
                {k.replace(/_/g, " ")}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              {keys.map((k) => (
                <td key={k} className={typeof r[k] === "number" ? "r" : undefined}>
                  {r[k] === null || r[k] === undefined ? "" : typeof r[k] === "object" ? JSON.stringify(r[k]) : String(r[k])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const statusKind = (s: string) => (s === "verified" || s === "ok" || s === "success" ? "verified" : s === "rejected" || s === "failed" || s === "error" ? "disputed" : s === "skipped" ? "stale" : "thin");

function RunDetail({ ws, id }: { ws: string; id: number }) {
  const { data, error } = useApi<{ run: RunInfo; traces: Trace[]; claims: ClaimRow[] }>(`/ws/${ws}/runs/${id}`);
  const [claimFilter, setClaimFilter] = useState<"all" | "verified" | "rejected" | "unverified">("all");
  const [trace, setTrace] = useState<number | null>(null);
  const claims = useMemo(() => (data?.claims ?? []).filter((c) => claimFilter === "all" || c.status === claimFilter), [data, claimFilter]);
  if (error) return <ErrorNote>{error}</ErrorNote>;
  if (!data)
    return (
      <div className="exhibit">
        <Sk w="40%" h={16} />
        <Sk h={120} style={{ marginTop: 20 }} />
      </div>
    );
  const r = data.run;
  const stats = r.stats ?? {};
  const dur = r.finished_at && r.started_at ? (new Date(r.finished_at).getTime() - new Date(r.started_at).getTime()) / 1000 : null;
  const counts = { verified: 0, rejected: 0, unverified: 0 } as Record<string, number>;
  data.claims.forEach((c) => (counts[c.status] = (counts[c.status] ?? 0) + 1));
  const collectors = Object.entries((stats.collectors ?? {}) as Record<string, Record<string, unknown>>);
  const agents = (stats.agents ?? {}) as { stages?: Record<string, unknown>[]; disagreements?: { description: string; claim_ids?: number[] }[] };
  let n = 1;
  return (
    <>
      <Exhibit n={n++} first title={`Run #${r.id}, ${r.kind} run of ${r.run_date}`} source={<>Started {fmtDateTime(r.started_at)} UTC · {r.mode} mode{stats.llm ? ` · LLM ${stats.llm}` : ""}.</>}>
        <Figures>
          <Figure label="Status" value={<span style={{ textTransform: "capitalize" }}>{r.status}</span>} tone={r.status === "failed" ? "bad" : r.status === "partial" ? "warn" : undefined} />
          <Figure label="LLM cost" value={`$${r.cost_usd.toFixed(4)}`} sub="free tiers are recorded at $0" />
          <Figure label="Tokens in / out" value={`${fmtInt(r.tokens_in / 1000)}k / ${fmtInt(r.tokens_out / 1000)}k`} sub={`${fmtInt(r.tokens_in)} / ${fmtInt(r.tokens_out)}`} />
          <Figure label="Duration" value={dur != null ? (dur > 90 ? `${(dur / 60).toFixed(1)} min` : `${dur.toFixed(1)} s`) : "–"} />
          <Figure label="Claims verified" value={`${counts.verified}/${data.claims.length}`} sub={`${counts.rejected} rejected · ${counts.unverified} withheld`} />
        </Figures>
      </Exhibit>

      {Array.isArray(stats.stages) && stats.stages.length ? (
        <Exhibit n={n++} title="Pipeline stages">
          <Table rows={stats.stages} />
        </Exhibit>
      ) : null}

      {collectors.length ? (
        <Exhibit n={n++} title="Collectors" source={<>Every fetch respects robots.txt; skipped collectors name the missing credential.</>}>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Collector</th>
                  <th>Status</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {collectors.map(([k, v]) => (
                  <tr key={k}>
                    <td style={{ fontWeight: 650, color: "var(--ink-strong)" }}>{k}</td>
                    <td>
                      <Mark kind={statusKind(String(v.status))}>{String(v.status)}</Mark>
                    </td>
                    <td className="muted">
                      {v.message
                        ? String(v.message)
                        : Object.entries(v)
                            .filter(([kk]) => kk !== "status" && kk !== "message")
                            .map(([kk, vv]) => `${kk.replace(/_/g, " ")} ${vv}`)
                            .join(" · ")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Exhibit>
      ) : null}

      {agents.stages?.length ? (
        <Exhibit n={n++} title="Agent state machine" source={<>Researcher → critic → analyst → writer; each stage can fail or run out of budget without aborting the run.</>}>
          <Table rows={agents.stages} />
          {agents.disagreements?.length ? (
            <div style={{ marginTop: 16 }}>
              <b style={{ fontSize: "var(--fs-13)" }}>Source disagreements recorded</b>
              <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                {agents.disagreements.map((d, i) => (
                  <li key={i}>
                    {d.description} <span className="hint">(claims {d.claim_ids?.join(", ")})</span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </Exhibit>
      ) : null}

      <Exhibit n={n++} title={`Agent traces (${data.traces.length})`} note={<>Select a step to read its tool calls.</>}>
        {data.traces.length ? (
          <>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Agent</th>
                    <th>Step</th>
                    <th>Status</th>
                    <th className="r">Tool calls</th>
                    <th className="r">Tokens in</th>
                    <th className="r">Tokens out</th>
                    <th className="r">Latency</th>
                    <th>Output</th>
                  </tr>
                </thead>
                <tbody>
                  {data.traces.map((t, i) => (
                    <tr key={i} className={`selectable${trace === i ? " selected" : ""}`} onClick={() => setTrace(trace === i ? null : i)}>
                      <td style={{ fontWeight: 650, color: "var(--ink-strong)" }}>{t.agent}</td>
                      <td>{t.step}</td>
                      <td>
                        <Mark kind={statusKind(t.status)}>{t.status}</Mark>
                      </td>
                      <td className="r">{t.tool_calls.length}</td>
                      <td className="r">{fmtInt(t.tokens_in)}</td>
                      <td className="r">{fmtInt(t.tokens_out)}</td>
                      <td className="r">{fmtInt(t.latency_ms)} ms</td>
                      <td className="muted clip">{t.error ? <span style={{ color: "var(--disputed)" }}>{t.error}</span> : t.output}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {trace !== null ? (
              <div style={{ marginTop: 14 }}>
                <b style={{ fontSize: "var(--fs-13)" }}>
                  Tool calls for {data.traces[trace].agent} · {data.traces[trace].step}
                </b>
                <pre className="log" style={{ marginTop: 8 }}>
                  {JSON.stringify(data.traces[trace].tool_calls, null, 1).slice(0, 20000)}
                </pre>
              </div>
            ) : null}
          </>
        ) : (
          <p className="hint">No agent traces for this run (baseline runs only collect and process).</p>
        )}
      </Exhibit>

      <Exhibit
        n={n++}
        title="Claims and critic decisions"
        tools={
          <Seg
            label="Claim status"
            value={claimFilter}
            options={["all", "verified", "rejected", "unverified"] as const}
            onChange={setClaimFilter}
            format={(s) => (s === "unverified" ? `Withheld (${counts.unverified})` : s === "all" ? `All (${data.claims.length})` : `${s[0].toUpperCase()}${s.slice(1)} (${counts[s]})`)}
          />
        }
        source={<>Only verified claims can appear in outputs. Stage 1 checks are deterministic (source, excerpt, numbers, names); stage 2 is an entailment judgement.</>}
      >
        {claims.length ? (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Type</th>
                  <th>Status</th>
                  <th>Claim</th>
                  <th>Critic</th>
                  <th>Stage 2</th>
                </tr>
              </thead>
              <tbody>
                {claims.map((c) => (
                  <tr key={c.id}>
                    <td className="muted tabular">#{c.id}</td>
                    <td>{c.type}</td>
                    <td>
                      <Mark kind={statusKind(c.status)}>{c.status === "unverified" ? "withheld" : c.status}</Mark>
                    </td>
                    <td className="clip" style={{ maxWidth: "52ch" }}>
                      {c.text}
                    </td>
                    <td className="muted clip">{c.critic}</td>
                    <td className="muted">{String(c.stage2 ?? "")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="hint">No claims with this status.</p>
        )}
      </Exhibit>

      <Disclosure label="Raw run statistics (JSON)">
        <pre className="log" style={{ marginTop: 8 }}>
          {JSON.stringify(stats, null, 1)}
        </pre>
      </Disclosure>
    </>
  );
}

const kindLabel = (k: string) => (k === "daily" ? "analysis" : k);

export default function Inspector() {
  const { ws } = useShell();
  const { data, error } = useApi<{ runs: RunInfo[] }>(`/ws/${ws}/runs`);
  const [sel, setSel] = useState<number | null>(null);
  const [kind, setKind] = useState<"analysis" | "pulse" | "all">("analysis");
  if (error) return <ErrorNote>{error}</ErrorNote>;
  if (!data) return <PageSkeleton />;
  const all = data.runs;
  if (!all.length)
    return (
      <>
        <PageHead title="No runs yet." />
        <Empty title="Runs appear here as soon as one starts." hint="Live collection starts runs on its own; you can also start one under Workspaces." />
      </>
    );
  const shown = kind === "all" ? all : all.filter((r) => kindLabel(r.kind) === kind);
  const runs = shown.length ? shown : all;
  const cur = runs.find((r) => r.id === sel) ?? runs[0];
  const failed = runs.filter((r) => r.status === "failed").length;
  const pulses = all.filter((r) => r.kind === "pulse").length;
  return (
    <>
      <PageHead
        title={`${kindLabel(cur.kind) === "pulse" ? "Source check" : "Analysis"} #${cur.id} ${cur.status === "success" ? "completed" : `ended ${cur.status}`} on ${cur.run_date}, ${cur.mode === "offline" ? "fixture data" : "live sources"}.`}
        deck={`${plural(all.length - pulses, "analysis run", "analysis runs")} and ${plural(pulses, "source check")} recorded${failed ? `; ${failed} of the shown runs failed` : ""}. Every stage, agent step, tool call and critic decision is kept for inspection.`}
      >
        <div className="field">
          <span>Show</span>
          <Seg
            label="Run kind"
            value={kind}
            options={["analysis", "pulse", "all"] as const}
            format={(v) => (v === "analysis" ? "Analyses" : v === "pulse" ? "Source checks" : "All runs")}
            onChange={(v) => {
              setKind(v);
              setSel(null);
            }}
          />
        </div>
        <label className="field" style={{ minWidth: 320 }}>
          <span>Run</span>
          <select className="select" value={cur.id} onChange={(e) => setSel(Number(e.target.value))}>
            {runs.map((r) => (
              <option key={r.id} value={r.id}>
                #{r.id} · {r.run_date} · {kindLabel(r.kind)} · {r.mode} · {r.status}
              </option>
            ))}
          </select>
        </label>
      </PageHead>
      <RunDetail ws={ws!} id={cur.id} key={cur.id} />
    </>
  );
}
