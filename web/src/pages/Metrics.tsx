import { useMemo, useState } from "react";
import { useApi, type MetricsResp } from "../api";
import { HBars, Lines } from "../components/charts";
import { Empty, ErrorNote, Exhibit, PageHead, PageSkeleton, Seg } from "../components/ui";
import { useShell } from "../shell";
import { compact, entityColor, entityOrder, fmtNum, fmtPeriod, fmtSigned } from "../util";

export default function Metrics() {
  const { ws, ctx } = useShell();
  const { data, error } = useApi<MetricsResp>(`/ws/${ws}/metrics`);
  const [conn, setConn] = useState<string | null>(null);
  const [regions, setRegions] = useState<string[] | null>(null);

  const summ = data?.summaries.find((s) => s.name === conn) ?? data?.summaries[0];
  const pts = useMemo(() => (data?.points ?? []).filter((p) => p.connector === summ?.name), [data, summ]);
  const allRegions = useMemo(() => [...new Set(pts.map((p) => p.region))].sort(), [pts]);
  const sel = regions ?? allRegions;
  const sub = pts.filter((p) => sel.includes(p.region));
  const entities = useMemo(() => entityOrder(ctx?.competitors ?? [], [...new Set(pts.map((p) => p.entity))].sort()), [ctx, pts]);

  if (error) return <ErrorNote>{error}</ErrorNote>;
  if (!data || !ctx) return <PageSkeleton figures={false} />;
  if (!data.configured)
    return (
      <>
        <PageHead title="No metrics connector is configured for this workspace." />
        <Empty
          title="Market metrics come from a connector you supply."
          hint="Add a csv_file or http_json connector to the workspace YAML (see docs/adding-a-workspace.md); FieldNote computes trends, shares and movers in code."
        />
      </>
    );
  if (!summ)
    return (
      <>
        <PageHead title="A connector is configured, but no data has been loaded yet." />
        <Empty
          title="Load the connector's data, then run the pipeline."
          hint="Place the CSV at the configured path (downloaded manually, respecting the source's terms) and run the pipeline."
        />
      </>
    );

  const periods = [...new Set(sub.map((p) => p.period))].sort();
  const latest = summ.latest_period ?? periods[periods.length - 1];
  const sum = (f: (p: (typeof sub)[number]) => boolean) => sub.filter(f).reduce((a, p) => a + p.value, 0);
  const totalLatest = sum((p) => p.period === latest);
  const shares = entities
    .map((e) => ({ e, v: sum((p) => p.period === latest && p.entity === e) }))
    .filter((x) => x.v > 0)
    .map((x) => ({ ...x, share: totalLatest ? (x.v / totalLatest) * 100 : 0 }))
    .sort((a, b) => b.share - a.share);
  const leader = shares[0];
  const movers = summ.entities
    .filter((e) => summ.movers.includes(e.entity))
    .sort((a, b) => Math.abs(b.pct_change ?? 0) - Math.abs(a.pct_change ?? 0));
  const topMover = movers[0];
  const byRegion = allRegions
    .filter((r) => sel.includes(r))
    .map((r) => ({ r, v: sum((p) => p.period === latest && p.region === r) }))
    .sort((a, b) => b.v - a.v);

  const series = entities
    .filter((e) => sub.some((p) => p.entity === e))
    .map((e) => ({
      name: e,
      color: entityColor(entities, e),
      emphasis: e === (topMover?.entity ?? leader?.e),
      points: periods.map((p) => ({ x: p, y: sum((q) => q.period === p && q.entity === e) })),
    }));

  const title = leader
    ? `${leader.e} leads ${summ.label} with ${fmtNum(leader.share)}% in ${fmtPeriod(latest)}${
        topMover?.pct_change != null ? `; ${topMover.entity} moved most (${fmtSigned(topMover.pct_change)}% month over month)` : ""
      }.`
    : `No ${summ.label} recorded for the selected regions.`;
  const source = (
    <>
      {summ.source_url ? (
        <a href={summ.source_url} target="_blank" rel="noreferrer">
          {summ.source_url}
        </a>
      ) : (
        "User-supplied file"
      )}
      ; unit: {summ.unit}. Sums, shares and month-over-month changes are computed by FieldNote, never by the model.
    </>
  );
  const toggle = (r: string) => setRegions(sel.includes(r) ? sel.filter((x) => x !== r) : [...sel, r]);

  return (
    <>
      <PageHead
        title={title}
        deck={`Latest period ${fmtPeriod(latest)} across ${sel.length} of ${allRegions.length} regions: ${compact(totalLatest)} ${summ.unit_short} in total.`}
      />
      <div className="toolbar" style={{ marginTop: 28 }}>
        {data.summaries.length > 1 ? (
          <div className="field">
            <span>Connector</span>
            <Seg label="Connector" value={summ.name} options={data.summaries.map((s) => s.name)} onChange={setConn} />
          </div>
        ) : null}
        <div className="field">
          <span>Regions</span>
          <div className="chips">
            {allRegions.map((r) => (
              <button key={r} type="button" className="chip" aria-pressed={sel.includes(r)} onClick={() => toggle(r)}>
                {r}
              </button>
            ))}
          </div>
        </div>
      </div>

      <Exhibit
        n={1}
        title={
          topMover?.pct_change != null
            ? `${topMover.entity} moved most in ${fmtPeriod(latest)}: ${fmtSigned(topMover.pct_change)}% month over month.`
            : `${summ.label[0].toUpperCase()}${summ.label.slice(1)} by entity, month by month.`
        }
        source={source}
        note={<>Highlighted: the biggest mover. Latest values at the line ends.</>}
      >
        {series.length ? (
          <Lines series={series} xs={periods} yFormat={(n) => compact(n)} xFormat={fmtPeriod} valueFormat={(n) => compact(n)} labelW={170} />
        ) : (
          <Empty title="No data for the selected regions." hint="Select at least one region above." />
        )}
      </Exhibit>

      {shares.length ? (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(340px, 1fr))", columnGap: 48 }}>
          <Exhibit
            n={2}
            title={`${leader?.e} holds ${fmtNum(leader?.share ?? 0)}% of ${summ.label} in ${fmtPeriod(latest)}.`}
            source={<>Share of the selected regions' total for {fmtPeriod(latest)}.</>}
          >
            <HBars
              rows={shares.map((s) => ({
                label: s.e,
                value: s.share,
                color: entityColor(entities, s.e),
                highlight: s.e === leader?.e,
                valueLabel: `${fmtNum(s.share)}%`,
              }))}
            />
          </Exhibit>
          <Exhibit
            n={3}
            title={byRegion[0] ? `${byRegion[0].r} is the largest region: ${compact(byRegion[0].v)} ${summ.unit_short}.` : `${summ.label} by region`}
            source={<>Selected regions, {fmtPeriod(latest)}. Highlighted: the largest region.</>}
          >
            <HBars
              rows={byRegion.map((r, i) => ({ label: r.r, value: r.v, highlight: i === 0, valueLabel: compact(r.v) }))}
              labelWidth={120}
            />
          </Exhibit>
        </div>
      ) : null}

      {movers.length ? (
        <Exhibit
          n={4}
          title={(() => {
            const up = [...movers].sort((a, b) => (b.pct_change ?? 0) - (a.pct_change ?? 0))[0];
            const down = [...movers].sort((a, b) => (a.pct_change ?? 0) - (b.pct_change ?? 0))[0];
            return `${up.entity} gained most (${fmtSigned(up.pct_change ?? 0)}%)${down && (down.pct_change ?? 0) < 0 ? `; ${down.entity} fell ${fmtSigned(down.pct_change ?? 0)}%` : ""}.`;
          })()}
          source={
            <>
              {fmtPeriod(summ.previous_period)} to {fmtPeriod(summ.latest_period)}, all regions. Share is of the latest period's total.
            </>
          }
        >
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Entity</th>
                  <th className="r">Latest</th>
                  <th className="r">Previous</th>
                  <th className="r">Change</th>
                  <th className="r">Share</th>
                </tr>
              </thead>
              <tbody>
                {movers.map((m) => (
                  <tr key={m.entity}>
                    <td>
                      <span className="entity">
                        <i className="swatch" style={{ background: entityColor(entities, m.entity) }} />
                        {m.entity}
                      </span>
                    </td>
                    <td className="r">{fmtNum(m.latest, 0)}</td>
                    <td className="r muted">{m.previous != null ? fmtNum(m.previous, 0) : "–"}</td>
                    <td className="r" style={{ fontWeight: 650, color: (m.pct_change ?? 0) >= 0 ? "var(--verified)" : "var(--rust)" }}>
                      {m.pct_change != null ? `${fmtSigned(m.pct_change)}%` : "–"}
                    </td>
                    <td className="r">{m.share != null ? `${fmtNum(m.share)}%` : "–"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Exhibit>
      ) : null}
    </>
  );
}
