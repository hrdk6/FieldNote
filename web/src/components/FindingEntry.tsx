import { motion } from "motion/react";
import { ArrowUpRight, TriangleAlert } from "lucide-react";
import { CRITERIA, CRITERION_LABEL, type Finding } from "../api";
import { CRIT_COLOR, cap } from "../util";
import { Disclosure, EASE, EvidenceList, Mark, StatusMarks, useDrawIn } from "./ui";

export function CategoryMark({ category }: { category: string }) {
  return (
    <Mark>
      {category === "risk" ? <TriangleAlert size={12} strokeWidth={2.2} /> : <ArrowUpRight size={12} strokeWidth={2.2} />}
      {cap(category)}
    </Mark>
  );
}

export function FindingEntry({ f, index }: { f: Finding; index: number }) {
  const { ref, shown, instant } = useDrawIn<HTMLDivElement>();
  return (
    <article className="finding">
      <div
        className="rank"
        aria-label={`Rank ${f.rank ?? index + 1}`}
        style={(f.rank ?? index + 1) === 1 ? { color: "var(--rust)", fontWeight: 400 } : undefined}
      >
        {f.rank ?? index + 1}
      </div>
      <div>
        <h3>{f.title}</h3>
        <div className="marks">
          <CategoryMark category={f.category} />
          <Mark kind="plain">
            {f.evidence_count} source{f.evidence_count === 1 ? "" : "s"}
          </Mark>
          <StatusMarks status={f.status} disputed={f.disputed} />
        </div>
        <dl className="memo">
          <dt>What happened</dt>
          <dd>{f.what}</dd>
          <dt>
            Why it matters
            <small>Analysis</small>
          </dt>
          <dd>{f.why}</dd>
          <dt>Recommended action</dt>
          <dd>{f.action}</dd>
        </dl>
        {f.counter_evidence.length ? (
          <p className="hint" style={{ marginTop: 12, color: "var(--disputed)" }}>
            Counter-evidence: {f.counter_evidence.join("; ")}
          </p>
        ) : null}
        <Disclosure label={`Evidence (${f.evidence.length}) and how to execute`}>
          <div className="howto">
            <h4>How to execute</h4>
            <p>{f.how}</p>
          </div>
          <EvidenceList items={f.evidence} />
        </Disclosure>
      </div>
      <div className="score-panel" ref={ref}>
        <div className="score-total">
          <span className="v">{(f.total ?? 0).toFixed(2)}</span>
          <span className="l">weighted score</span>
        </div>
        {CRITERIA.map((c, i) => (
          <div className="crit" key={c}>
            <span>{CRITERION_LABEL[c]}</span>
            <span className="val">{f.scores[c].toFixed(2)}</span>
            <span className="bar">
              <motion.i
                style={{ width: `${Math.round(f.scores[c] * 100)}%`, background: CRIT_COLOR[c], originX: 0 }}
                initial={{ scaleX: 0 }}
                animate={{ scaleX: shown ? 1 : 0 }}
                transition={instant ? { duration: 0 } : { duration: 0.6, ease: EASE, delay: 0.08 * i }}
              />
            </span>
          </div>
        ))}
      </div>
    </article>
  );
}
