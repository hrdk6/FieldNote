import { useMemo, useState } from "react";
import { useApi, type VoiceResp } from "../api";
import { Heatmap, PairedBars, RangeDots } from "../components/charts";
import { Disclosure, Empty, ErrorNote, Exhibit, Mark, PageHead, PageSkeleton } from "../components/ui";
import { useShell } from "../shell";
import { entityOrder, fmtSigned, humanize, plural } from "../util";

const sentiment = (v: number) => `${fmtSigned(v, 2)}`;

export default function Voice() {
  const { ws, ctx } = useShell();
  const { data, error } = useApi<VoiceResp>(`/ws/${ws}/voice`);
  const [metric, setMetric] = useState<string | null>(null);

  const aspects = useMemo(() => [...new Set((data?.heat ?? []).map((h) => h.aspect))].sort(), [data]);
  const brands = useMemo(
    () => entityOrder(data?.competitors ?? [], (data?.heat ?? []).map((h) => h.entity)).filter((e) => data?.heat.some((h) => h.entity === e)),
    [data],
  );

  if (error) return <ErrorNote>{error}</ErrorNote>;
  if (!data || !ctx) return <PageSkeleton figures={false} />;

  const themes = [...data.themes].sort((a, b) => b.size - a.size);
  const loudest = themes[0];
  const emerging = themes.filter((t) => t.emerging);
  const metrics = [...new Set(data.cvr.map((r) => r.metric))];
  const m = metric ?? metrics[0];
  const cvr = data.cvr.filter((r) => r.metric === m && r.median != null && r.q1 != null && r.q3 != null).sort((a, b) => b.n - a.n);
  const claimsOnly = data.cvr.filter((r) => r.metric === m && r.median == null);

  const worst = [...data.heat].filter((h) => h.n >= 2).sort((a, b) => a.mean - b.mean)[0];
  const title = loudest
    ? `“${loudest.label}” is the loudest customer theme (${plural(loudest.size, "post")}, sentiment ${sentiment(loudest.sentiment)}).`
    : data.heat.length
      ? `Customers are least positive about ${worst?.aspect ?? "one aspect"}${worst ? ` for ${worst.entity}` : ""}.`
      : "No customer-voice data yet.";

  if (!themes.length && !data.heat.length && !data.cvr.length)
    return (
      <>
        <PageHead title={title} />
        <Empty
          title="Customer voice needs Reddit or YouTube collection."
          hint="Set REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET or YOUTUBE_API_KEY and run the pipeline; the offline demo uses fixtures."
        />
      </>
    );

  let n = 0;
  return (
    <>
      <PageHead
        title={title}
        deck={`${plural(data.tags_total, "tagged post")} in the last 14 days${emerging.length ? `; ${plural(emerging.length, "emerging theme")}` : ""}. Customer voice is a sampled, non-representative slice of public discussion; read magnitudes with care.`}
      />

      {themes.length ? (
        <Exhibit
          n={++n}
          first
          title={
            emerging.length
              ? `${plural(emerging.length, "theme")} emerged this week, led by “${[...emerging].sort((a, b) => b.size - a.size)[0].label}”.`
              : `“${loudest.label}” leads theme volume with ${plural(loudest.size, "post")}.`
          }
          note={<>Grey: previous 7 days. Blue: current 7 days. Rust: emerging.</>}
          source={<>Public posts clustered into themes each run; emerging means fast growth in share of voice from a small base.</>}
        >
          <PairedBars
            rows={themes.map((t) => ({
              label: t.label,
              prev: t.size_prev,
              cur: t.size,
              highlight: t.emerging,
              tip: (
                <div>
                  <b>{t.label}</b>
                  <div>
                    {t.size} posts (was {t.size_prev}) · mean sentiment {sentiment(t.sentiment)}
                  </div>
                  <div className="hint">Keywords: {t.keywords.slice(0, 6).join(", ")}</div>
                </div>
              ),
            }))}
          />
          <Disclosure label="Read the source posts behind each theme">
            <div style={{ marginTop: 8 }}>
              {themes.map((t) => (
                <div key={t.id} style={{ padding: "14px 0", borderTop: "1px solid var(--rule-soft)" }}>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
                    <b style={{ color: "var(--ink-strong)" }}>{t.label}</b>
                    {t.emerging ? <Mark kind="thin">Emerging</Mark> : null}
                    <span className="hint">
                      {t.size} posts · sentiment {sentiment(t.sentiment)} · {t.keywords.slice(0, 6).join(", ")}
                    </span>
                  </div>
                  <ul style={{ margin: "8px 0 0", paddingLeft: 18, display: "grid", gap: 6 }}>
                    {t.samples.map((s, i) => (
                      <li key={i} style={{ color: "var(--ink-2)", fontSize: "var(--fs-14)" }}>
                        “{s.text}”{" "}
                        <a href={s.url} target="_blank" rel="noreferrer">
                          {s.source}
                        </a>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          </Disclosure>
        </Exhibit>
      ) : null}

      {data.heat.length ? (
        <Exhibit
          n={++n}
          first={!themes.length}
          title={
            worst
              ? `${humanize(worst.aspect)} for ${worst.entity} is the most negative pairing (${sentiment(worst.mean)}, ${plural(worst.n, "post")}).`
              : "Sentiment by aspect and brand."
          }
          source={<>Mean sentiment of posts tagged with each aspect and brand, last 14 days (−1 negative to +1 positive).</>}
          note={<>Cells with few posts swing easily; hover a cell for its sample size.</>}
        >
          <Heatmap
            rows={aspects}
            rowFormat={humanize}
            cols={brands}
            cell={(a, e) => {
              const h = data.heat.find((x) => x.aspect === a && x.entity === e);
              return h ? { v: h.mean, n: h.n } : undefined;
            }}
          />
        </Exhibit>
      ) : null}

      {data.cvr.length ? (
        <Exhibit
          n={++n}
          title={(() => {
            const g = cvr
              .filter((r) => r.claimed != null && !r.low)
              .sort((a, b) => Math.abs((b.claimed ?? 0) - (b.median ?? 0)) - Math.abs((a.claimed ?? 0) - (a.median ?? 0)))[0];
            return g
              ? `${g.entity} owners report ${g.median} ${g.unit} of ${m.replace(/_/g, " ")} against a claimed ${g.claimed} ${g.unit}.`
              : `Claimed versus owner-reported ${m.replace(/_/g, " ")}.`;
          })()}
          tools={
            metrics.length > 1 ? (
              <span className="chips">
                {metrics.map((x) => (
                  <button key={x} type="button" className="chip" aria-pressed={x === m} onClick={() => setMetric(x)}>
                    {x.replace(/_/g, " ")}
                  </button>
                ))}
              </span>
            ) : null
          }
          source={<>Brand claims from their own pages; owner reports extracted from public posts, with the middle half (IQR) shown.</>}
          note={<>Grey rows have fewer reports than the configured minimum and are never stated as fact.</>}
        >
          {cvr.length ? (
            <RangeDots
              unit={cvr[0].unit}
              rows={cvr.map((r) => ({
                label: `${r.entity} (n=${r.n})`,
                q1: r.q1!,
                q3: r.q3!,
                median: r.median!,
                claimed: r.claimed,
                low: r.low,
                tip: (
                  <div>
                    <b>{r.entity}</b>
                    <div>
                      Median {r.median} {r.unit} (IQR {r.q1}–{r.q3}, n={r.n}){r.low ? " · low confidence" : ""}
                    </div>
                    {r.claimed != null ? (
                      <div>
                        Claimed {r.claimed} {r.unit}
                      </div>
                    ) : null}
                  </div>
                ),
              }))}
            />
          ) : (
            <Empty title="No owner reports yet for this metric." hint="Claims were found, but no public posts reported a comparable value." />
          )}
          {claimsOnly.length ? (
            <p className="hint" style={{ marginTop: 12 }}>
              Claims without owner reports: {claimsOnly.map((r) => `${r.entity} ${r.claimed} ${r.unit}`).join(" · ")}.
            </p>
          ) : null}
          {cvr.some((r) => r.samples.length) ? (
            <Disclosure label="Source posts behind the reported values">
              <div className="table-wrap" style={{ marginTop: 8 }}>
                <table className="table">
                  <thead>
                    <tr>
                      <th>Entity</th>
                      <th className="r">Value</th>
                      <th>Excerpt</th>
                      <th className="r">Document</th>
                    </tr>
                  </thead>
                  <tbody>
                    {cvr.flatMap((r) =>
                      r.samples.map((s, i) => (
                        <tr key={`${r.entity}-${i}`}>
                          <td>{i === 0 ? <b>{r.entity}</b> : null}</td>
                          <td className="r">
                            {s.value} {r.unit}
                          </td>
                          <td className="clip">“{s.excerpt}”</td>
                          <td className="r muted">#{s.document_id}</td>
                        </tr>
                      )),
                    )}
                  </tbody>
                </table>
              </div>
            </Disclosure>
          ) : null}
        </Exhibit>
      ) : null}
    </>
  );
}
