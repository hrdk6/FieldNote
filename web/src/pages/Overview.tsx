import { Link } from "react-router-dom";
import { ArrowRight } from "lucide-react";
import { CRITERIA, CRITERION_LABEL, useApi, type Overview as O } from "../api";
import { CountUp, Empty, ErrorNote, Exhibit, Figure, Figures, PageHead, PageSkeleton, Spinner } from "../components/ui";
import { FindingEntry } from "../components/FindingEntry";
import { LiveFeed } from "../components/LiveFeed";
import { ago, elapsed, until, useLive, useNow } from "../live";
import { runLine, useShell } from "../shell";
import { fmtInt, plural } from "../util";

const int = (n: number) => fmtInt(n);
const pct = (n: number) => `${Math.round(n)}%`;

export default function Overview() {
  const { ws, ctx } = useShell();
  const { status, active } = useLive();
  const now = useNow(active ? 1000 : 30000);
  const { data, error } = useApi<O>(`/ws/${ws}/overview`);
  if (error) return <ErrorNote>{error}</ErrorNote>;
  if (!data || !ctx) return <PageSkeleton />;
  const k = data.kpis;
  const lead = data.top[0];
  const compCounts = Object.entries(k.changes_by_competitor).sort((a, b) => b[1] - a[1]);
  const topComp = compCounts.length > 1 && compCounts[0][1] === compCounts[1][1] ? null : compCounts[0];
  const run = ctx.run;
  const weights = CRITERIA.map((c) => `${CRITERION_LABEL[c].toLowerCase()} ${Math.round((ctx.weights[c] ?? 0) * 100)}%`).join(", ");
  const t = status?.totals;
  const stale = status?.health === "stale";

  const title = lead
    ? `The lead ${lead.category}: ${lead.title.replace(/\.$/, "")}.`
    : active
      ? `Collecting live data for ${ctx.display_name}.`
      : run
        ? "No findings passed the critic in the latest analysis."
        : `Waiting for the first analysis of ${ctx.display_name}.`;

  return (
    <>
      <PageHead
        title={title}
        deck={
          <>
            {t ? `${plural(t.documents_24h, "item")} collected in the last 24 hours, ` : ""}
            {plural(k.changes_7d, "competitor page change")} this week and {plural(k.open_actions, "open action")}. {runLine(ctx)}
          </>
        }
      />

      <Exhibit
        n={1}
        first
        title={
          t
            ? `${fmtInt(t.documents_24h)} new items and ${plural(t.changes_24h, "page change")} in the last 24 hours${k.checked ? `; ${Math.round(k.verified_pct)}% of checked claims verified` : ""}.`
            : `${fmtInt(k.documents_7d)} new documents and ${plural(k.changes_7d, "page change")} this week.`
        }
        source={
          <>
            ¹ Every document collected for this workspace from live sources. ² Competitor pages are diffed against their previous
            snapshot on every collection. ³ Claims checked by the critic{run ? ` in analysis #${run.id}` : ""}; only verified claims
            reach a brief. ⁴ Extracted from pasted meeting notes.
          </>
        }
      >
        <Figures>
          <Figure
            label="Documents collected"
            value={<CountUp value={t?.documents ?? k.documents} format={int} />}
            fn={1}
            sub={t ? `${fmtInt(t.documents_24h)} in the last 24 hours` : `${fmtInt(k.documents_7d)} in the last 7 days`}
          />
          <Figure
            label="Page changes, 7 days"
            value={<CountUp value={k.changes_7d} format={int} />}
            fn={2}
            sub={
              topComp
                ? `Most: ${topComp[0]} (${topComp[1]})`
                : compCounts.length
                  ? `Across ${compCounts.length} competitors`
                  : `${ctx.competitors.length} competitors watched`
            }
          />
          <Figure
            label="Verified claims"
            value={k.checked ? <CountUp value={k.verified_pct} format={pct} /> : "–"}
            fn={3}
            tone={k.checked && k.verified_pct < 50 ? "warn" : undefined}
            sub={k.checked ? `of ${fmtInt(k.checked)} checked in the latest analysis` : "no analysis yet"}
          />
          <Figure
            label="Open actions"
            value={<CountUp value={k.open_actions} format={int} />}
            fn={4}
            tone={k.overdue_actions ? "bad" : undefined}
            sub={k.overdue_actions ? `${k.overdue_actions} overdue` : "none overdue"}
          />
          <Figure
            label="Sources checked"
            value={
              active ? (
                <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
                  Now <Spinner />
                </span>
              ) : status?.updated_at ? (
                ago(status.updated_at, now)
              ) : (
                "Never"
              )
            }
            tone={stale ? "warn" : undefined}
            sub={
              active
                ? `${active.kind === "pulse" ? "collecting" : "analysing"} for ${elapsed(active.started_at, now)}`
                : status?.scheduler.enabled
                  ? `next check ${until(status.next_pulse, now)}`
                  : "live collection is off"
            }
          />
        </Figures>
      </Exhibit>

      <Exhibit
        n={2}
        title={
          status?.updated_at
            ? `The newest from news, competitor pages and discussions, checked ${ago(status.updated_at, now)}.`
            : "The newest items from live sources."
        }
        source={
          <>
            The workspace's news searches and feeds, its watched competitor pages and, when API keys are set, Reddit and YouTube;
            robots.txt is respected. This feed updates by itself as new items arrive.
          </>
        }
      >
        <LiveFeed ws={ws!} entities={ctx.competitors} />
      </Exhibit>

      <Exhibit
        n={3}
        title={
          data.top.length
            ? (() => {
                const risks = data.top.filter((f) => f.category === "risk").length;
                const opps = data.top.length - risks;
                const thin = data.top.filter((f) => f.status === "demoted").length;
                const mix = [opps ? plural(opps, "opportunity", "opportunities") : "", risks ? plural(risks, "risk") : ""].filter(Boolean).join(" and ");
                return `${mix} make the top ${data.top.length}${thin ? `; ${thin} ${thin === 1 ? "rests" : "rest"} on thin evidence` : ", all on sufficient evidence"}.`;
              })()
            : "Top findings"
        }
        tools={
          data.top.length ? (
            <Link className="btn quiet sm" to={`/${ws}/board`}>
              Re-weight on the board <ArrowRight size={14} />
            </Link>
          ) : null
        }
        source={
          data.top.length ? (
            <>
              Verified claims from analysis #{run?.id}. Weighted score: {weights}. Ties break by evidence count, then recency.
              {status?.scheduler.enabled ? ` Analysis repeats every ${status.scheduler.analysis_hours} hours; next ${until(status.next_analysis, now)}.` : ""}
            </>
          ) : undefined
        }
      >
        {data.top.length ? (
          <div className="findings">
            {data.top.map((f, i) => (
              <FindingEntry key={f.id} f={f} index={i} />
            ))}
          </div>
        ) : active?.kind === "analysis" ? (
          <Empty
            title="The analysis is running now."
            hint={`The researcher, critic, analyst and writer are working through the collected sources (${elapsed(active.started_at, now)} so far). Free model tiers are rate limited, so this can take several minutes; findings appear here when it finishes.`}
          />
        ) : (
          <Empty
            title="No findings yet for this workspace."
            hint={
              status?.scheduler.enabled
                ? `Findings appear after an analysis whose claims pass the critic. The next analysis is due ${until(status.next_analysis, now)}.`
                : "Findings appear after an analysis whose claims pass the critic. Start one from the Workspaces page."
            }
          >
            <Link className="btn primary" to={`/${ws}/workspaces?tab=this`}>
              Open live collection
            </Link>
          </Empty>
        )}
      </Exhibit>
    </>
  );
}
