import { useMemo } from "react";
import { useApi, type EvalRow } from "../api";
import { Lines, SplitBars } from "../components/charts";
import { CountUp, Disclosure, Empty, ErrorNote, Exhibit, Figure, Figures, Mark, PageHead, PageSkeleton } from "../components/ui";
import { renderMarkdown } from "../markdown";
import { useShell } from "../shell";
import { fmtDateTime } from "../util";

const pct = (n: number) => `${Math.round(n * 100)}%`;

export default function Evals() {
  const { ws } = useShell();
  const { data, error } = useApi<{ history: EvalRow[]; report: string; source?: string }>(`/ws/${ws}/evals`);
  const report = useMemo(() => (data?.report ? renderMarkdown(data.report) : ""), [data]);
  if (error) return <ErrorNote>{error}</ErrorNote>;
  if (!data) return <PageSkeleton />;
  const hist = data.history;
  if (!hist.length)
    return (
      <>
        <PageHead title="No eval results yet." />
        <Empty title="Evals measure citation validity, critic catch rate, number consistency and ranking determinism." hint="Run `fieldnote eval` (or `fieldnote demo`)." />
      </>
    );
  const latest = hist[0];
  const m = latest.metrics;
  const checks = Object.entries((m.checks ?? {}) as Record<string, boolean>);
  const failing = checks.filter(([, v]) => !v);
  const title = m.citation_validity
    ? `The critic caught ${pct(m.adversarial.rate)} of corrupted claims; citation validity is ${pct(m.citation_validity.rate)}${latest.passed ? " and every threshold passed" : ""}.`
    : `Latest eval ${latest.passed ? "passed" : "failed"}.`;
  const series = ["citation_validity", "adversarial", "number_consistency"]
    .map((k, i) => ({
      name: k === "adversarial" ? "Critic catch rate" : k === "citation_validity" ? "Citation validity" : "Number consistency",
      color: ["var(--steel)", "var(--rust)", "var(--c3)"][i],
      emphasis: k === "adversarial",
      points: [...hist]
        .reverse()
        .filter((h) => h.metrics[k])
        .map((h) => ({ x: `#${h.id}`, y: h.metrics[k].rate as number })),
    }))
    .filter((s) => s.points.length);
  const xs = [...hist].reverse().filter((h) => h.metrics.citation_validity).map((h) => `#${h.id}`);
  const ex = m.extraction;

  let n = 1;
  return (
    <>
      <PageHead
        title={title}
        deck={
          <>
            Eval #{latest.id}, {fmtDateTime(latest.created)} UTC, on the {latest.mode} LLM path.{" "}
            <Mark kind={latest.passed ? "verified" : "disputed"}>{latest.passed ? "Pass" : "Fail"}</Mark>
            {data.source === "labelled" ? (
              <>
                {" "}
                These scores come from `fieldnote eval` on the labelled test set: they measure the pipeline's accuracy, not today's live data.
              </>
            ) : null}
          </>
        }
      />
      {m.citation_validity ? (
        <Exhibit
          n={n++}
          first
          title="Latest eval at a glance"
          source={<>¹ Claims whose excerpt is found in the cited document. ² Share of programmatically corrupted claims the critic rejects. ³ Numbers in claims that match or re-compute from their source.</>}
        >
          <Figures>
            <Figure label="Citation validity" fn={1} value={<CountUp value={m.citation_validity.rate} format={pct} />} sub={`${m.citation_validity.valid} of ${m.citation_validity.claims} claims`} />
            <Figure label="Critic catch rate" fn={2} value={<CountUp value={m.adversarial.rate} format={pct} />} tone={m.adversarial.rate < 0.8 ? "warn" : undefined} />
            <Figure label="Number consistency" fn={3} value={<CountUp value={m.number_consistency.rate} format={pct} />} />
            <Figure label="Ranking deterministic" value={m.ranking?.deterministic ? "Yes" : "No"} tone={m.ranking?.deterministic ? undefined : "bad"} />
            {ex ? <Figure label="Extraction P / R" value={`${pct(ex.precision)} / ${pct(ex.recall)}`} sub="action items from notes" /> : null}
          </Figures>
        </Exhibit>
      ) : null}

      {m.adversarial?.by_kind ? (
        <Exhibit n={n++} title="Corrupted claims caught, by type of corruption" source={<>Each verified claim is corrupted programmatically (wrong number, swapped entity, fake quote, …) and sent back through the critic.</>}>
          <SplitBars
            rows={Object.entries(m.adversarial.by_kind as Record<string, { caught: number; total: number }>).map(([k, v]) => ({
              label: k,
              a: v.caught,
              b: v.total - v.caught,
            }))}
          />
        </Exhibit>
      ) : null}

      {checks.length ? (
        <Exhibit n={n++} title={failing.length ? `${failing.length} of ${checks.length} threshold checks fail` : `All ${checks.length} threshold checks pass`} source={<>Thresholds from evals/datasets/thresholds.json.</>}>
          <div className="marks" style={{ gap: 8 }}>
            {checks.map(([k, v]) => (
              <Mark key={k} kind={v ? "verified" : "disputed"}>
                {k.replace(/_/g, " ")}
              </Mark>
            ))}
          </div>
        </Exhibit>
      ) : null}

      {xs.length > 1 ? (
        <Exhibit n={n++} title="History of the three headline rates" source={<>{xs.length} eval runs, oldest first.</>}>
          <Lines series={series} xs={xs} yDomain={[0, 1.05]} yFormat={(v) => `${Math.round(v * 100)}%`} height={260} labelW={150} />
        </Exhibit>
      ) : null}

      {report ? (
        <div style={{ marginTop: 40 }}>
          <Disclosure label="Full eval report (out/eval_report.md)">
            <article className="prose" style={{ marginTop: 16 }} dangerouslySetInnerHTML={{ __html: report }} />
          </Disclosure>
        </div>
      ) : null}
    </>
  );
}
