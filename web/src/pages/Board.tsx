/* Opportunity Board: the signature interaction. Moving a weight re-ranks the findings in the browser with the
   same deterministic function the briefs use; rows glide to their new places and the contribution bars morph. */

import { AnimatePresence, LayoutGroup, motion } from "motion/react";
import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowDown, ArrowUp, ChevronRight, RotateCcw } from "lucide-react";
import { CRITERIA, CRITERION_LABEL, useApi, type Criterion, type Finding } from "../api";
import { Empty, ErrorNote, EvidenceList, Exhibit, Mark, PageHead, PageSkeleton, Reveal, StatusMarks } from "../components/ui";
import { CategoryMark } from "../components/FindingEntry";
import { useShell } from "../shell";
import { CRIT_COLOR, contributions, fmtDate, rank, useReducedMotion } from "../util";

const STATUSES = ["active", "demoted", "stale"] as const;
const STATUS_LABEL: Record<string, string> = { active: "Active", demoted: "Thin evidence", stale: "Stale" };

function Row({
  f,
  weights,
  delta,
  open,
  onToggle,
  reduced,
}: {
  f: Finding & { rank: number; total: number };
  weights: Record<Criterion, number>;
  delta: number;
  open: boolean;
  onToggle: () => void;
  reduced: boolean;
}) {
  const parts = contributions(f.scores, weights);
  const spring = reduced ? { duration: 0 } : { type: "spring" as const, stiffness: 380, damping: 36, mass: 0.9 };
  return (
    <motion.div layout="position" transition={spring} className={`board-row${f.rank === 1 ? " top" : ""}`}>
      <button type="button" className="board-line" aria-expanded={open} onClick={onToggle}>
        <span className="board-rank">
          <AnimatePresence mode="popLayout" initial={false}>
            <motion.span
              key={f.rank}
              style={{ display: "inline-block" }}
              initial={reduced ? { opacity: 0 } : { y: delta > 0 ? 12 : -12, opacity: 0 }}
              animate={{ y: 0, opacity: 1 }}
              exit={reduced ? { opacity: 0 } : { y: delta > 0 ? -12 : 12, opacity: 0 }}
              transition={{ duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
            >
              {f.rank}
            </motion.span>
          </AnimatePresence>
          <AnimatePresence>
            {delta ? (
              <motion.span
                key={`${f.id}-${delta}-${f.rank}`}
                className={`move ${delta > 0 ? "up" : "down"}`}
                initial={{ opacity: 0, x: -3 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.18 }}
                aria-label={delta > 0 ? `up ${delta}` : `down ${-delta}`}
              >
                {delta > 0 ? <ArrowUp size={10} strokeWidth={3} /> : <ArrowDown size={10} strokeWidth={3} />}
                {Math.abs(delta)}
              </motion.span>
            ) : null}
          </AnimatePresence>
        </span>
        <span className="board-title">
          {f.title}
          <span className="marks">
            <CategoryMark category={f.category} />
            <StatusMarks status={f.status} disputed={f.disputed} />
          </span>
        </span>
        <span className="contrib" aria-label="Weighted contribution by criterion">
          {parts.map((p) => (
            <motion.i
              key={p.c}
              style={{ background: CRIT_COLOR[p.c] }}
              initial={false}
              animate={{ width: `${p.v * 100}%` }}
              transition={reduced ? { duration: 0 } : { duration: 0.42, ease: [0.16, 1, 0.3, 1] }}
            />
          ))}
        </span>
        <span className="board-total">{f.total.toFixed(3)}</span>
        <span className="r hide-sm">{f.evidence_count}</span>
        <span className="r hide-sm">{fmtDate(f.last_seen, false)}</span>
        <ChevronRight size={16} className="chev" style={{ color: "var(--ink-3)", transform: open ? "rotate(90deg)" : undefined, transition: "transform 200ms" }} />
      </button>
      <Reveal open={open}>
        <div className="board-detail">
          <dl className="memo" style={{ marginTop: 6 }}>
            <dt>What happened</dt>
            <dd>{f.what}</dd>
            <dt>
              Why it matters<small>Analysis</small>
            </dt>
            <dd>{f.why}</dd>
            <dt>Recommended action</dt>
            <dd>{f.action}</dd>
            <dt>How to execute</dt>
            <dd>{f.how}</dd>
            {Object.keys(f.raw).length ? (
              <>
                <dt>
                  Analyst ratings<small>1 to 5, with rationale</small>
                </dt>
                <dd>
                  <table className="table" style={{ fontSize: "var(--fs-13)" }}>
                    <tbody>
                      {(["market_size", "competitor_gap", "effort"] as const)
                        .filter((k) => f.raw[k])
                        .map((k) => (
                          <tr key={k}>
                            <td style={{ whiteSpace: "nowrap", fontWeight: 600 }}>{CRITERION_LABEL[k]}</td>
                            <td className="r" style={{ fontWeight: 700 }}>
                              {f.raw[k].value}
                            </td>
                            <td className="muted">{f.raw[k].rationale}</td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                </dd>
              </>
            ) : null}
          </dl>
          {f.counter_evidence.length ? (
            <p style={{ marginTop: 14 }}>
              <Mark kind="disputed">Counter-evidence</Mark> {f.counter_evidence.join("; ")}
            </p>
          ) : null}
          <h4 style={{ marginTop: 18, fontSize: "var(--fs-13)", color: "var(--ink-2)" }}>Evidence</h4>
          <EvidenceList items={f.evidence} />
        </div>
      </Reveal>
    </motion.div>
  );
}

export default function Board() {
  const { ws, ctx } = useShell();
  const reduced = useReducedMotion();
  const [statuses, setStatuses] = useState<string[]>(["active", "demoted"]);
  const q = statuses.length ? statuses.join(",") : "active";
  const { data, error } = useApi<{ findings: Finding[]; weights: Record<Criterion, number> }>(`/ws/${ws}/findings?statuses=${q}`);
  const defaults = data?.weights ?? ctx?.weights;
  const [weights, setWeights] = useState<Record<Criterion, number> | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const w = weights ?? defaults ?? { market_size: 0.25, competitor_gap: 0.25, evidence_strength: 0.25, effort: 0.25 };
  const allZero = CRITERIA.every((c) => (w[c] ?? 0) === 0);
  const effective = allZero && defaults ? defaults : w;

  const ranked = useMemo(() => rank(data?.findings ?? [], effective), [data, effective]);

  // Rank movement since the previous weights, shown briefly after each change.
  const prevRanks = useRef<Map<number, number>>(new Map());
  const [deltas, setDeltas] = useState<Map<number, number>>(new Map());
  useEffect(() => {
    const next = new Map<number, number>();
    const d = new Map<number, number>();
    ranked.forEach((f) => {
      next.set(f.id, f.rank);
      const before = prevRanks.current.get(f.id);
      if (before !== undefined && before !== f.rank) d.set(f.id, before - f.rank);
    });
    prevRanks.current = next;
    if (d.size) {
      setDeltas(d);
      const t = window.setTimeout(() => setDeltas(new Map()), 1800);
      return () => window.clearTimeout(t);
    }
  }, [ranked]);

  if (error) return <ErrorNote>{error}</ErrorNote>;
  if (!data || !ctx) return <PageSkeleton figures={false} />;

  const lead = ranked[0];
  const changed = weights !== null && CRITERIA.some((c) => Math.abs((weights[c] ?? 0) - (defaults?.[c] ?? 0)) > 1e-9);
  const wsum = CRITERIA.reduce((a, c) => a + (effective[c] ?? 0), 0) || 1;
  const title = lead
    ? `${lead.title.replace(/\.$/, "")} ranks first ${changed ? "under your weights" : "under the workspace weights"}.`
    : "The board is empty.";

  return (
    <>
      <PageHead
        title={title}
        deck={
          lead
            ? `${ranked.length} findings. Move a weight and the board re-ranks instantly from stored scores: the same deterministic sum the briefs use, with no model calls.`
            : "Findings appear after a run whose claims pass the critic."
        }
      />

      <Exhibit
        n={1}
        first
        title={(() => {
          const top2 = [...CRITERIA].sort((a, b) => (effective[b] ?? 0) - (effective[a] ?? 0)).slice(0, 2);
          const share = Math.round((top2.reduce((a, c) => a + (effective[c] ?? 0), 0) / wsum) * 100);
          return `${CRITERION_LABEL[top2[0]]} and ${CRITERION_LABEL[top2[1]].toLowerCase()} carry ${share}% of the score${changed ? " under your weights" : ""}.`;
        })()}
        tools={
          <button type="button" className="btn quiet sm" disabled={!changed} onClick={() => setWeights(null)}>
            <RotateCcw size={14} /> Reset to workspace weights
          </button>
        }
        note={
          allZero ? (
            <span style={{ color: "var(--thin)" }}>All weights are zero; the board is using the workspace defaults.</span>
          ) : (
            <>Weights are normalised to sum to 100%. Evidence strength is computed from distinct verified sources, source-type diversity, recency and primary sources.</>
          )
        }
      >
        <div className="weights">
          {CRITERIA.map((c) => (
            <label key={c} className="wt">
              <span className="wt-head">
                <span>
                  <i className="wt-key" style={{ background: CRIT_COLOR[c] }} />
                  {CRITERION_LABEL[c]}
                </span>
                <span className="v">{Math.round(((effective[c] ?? 0) / wsum) * 100)}%</span>
              </span>
              <input
                className="range"
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={w[c] ?? 0}
                style={{ ["--p" as string]: `${(w[c] ?? 0) * 100}%` }}
                onChange={(e) => setWeights({ ...w, [c]: Number(e.target.value) })}
                aria-valuetext={`${Math.round((w[c] ?? 0) * 100)} of 100`}
              />
            </label>
          ))}
          <div className="field">
            <span>Show</span>
            <div className="chips">
              {STATUSES.map((s) => (
                <button
                  key={s}
                  type="button"
                  className="chip"
                  aria-pressed={statuses.includes(s)}
                  onClick={() => setStatuses((l) => (l.includes(s) ? l.filter((x) => x !== s) : [...l, s]))}
                >
                  {STATUS_LABEL[s]}
                </button>
              ))}
            </div>
          </div>
        </div>
      </Exhibit>

      <Exhibit
        n={2}
        title={
          lead
            ? `${lead.title.replace(/\.$/, "")} leads at ${lead.total.toFixed(3)}; ${ranked.filter((f) => f.status === "demoted").length} of ${ranked.length} findings rest on thin evidence.`
            : "Findings ranked by weighted score."
        }
        source={<>Analyst ratings (1 to 5) normalised to 0–1; total = weighted sum. Ties break by evidence count, then recency, then title.</>}
      >
        {ranked.length ? (
          <div className="board" role="list">
            <div className="board-head" aria-hidden>
              <span>Rank</span>
              <span>Finding</span>
              <span>Contribution by criterion</span>
              <span style={{ textAlign: "right" }}>Score</span>
              <span style={{ textAlign: "right" }}>Sources</span>
              <span style={{ textAlign: "right" }}>Last seen</span>
              <span />
            </div>
            <LayoutGroup>
              {ranked.map((f) => (
                <Row
                  key={f.id}
                  f={f}
                  weights={effective}
                  delta={deltas.get(f.id) ?? 0}
                  open={open === f.id}
                  onToggle={() => setOpen((o) => (o === f.id ? null : f.id))}
                  reduced={reduced}
                />
              ))}
            </LayoutGroup>
          </div>
        ) : (
          <Empty title="No findings with the selected statuses." hint="Include thin-evidence or stale findings above, or run the pipeline." />
        )}
      </Exhibit>
    </>
  );
}
