/* Exhibit charts: hand-set SVG with direct labels, one highlighted series, and a single draw-in the first
   time each chart scrolls into view. Colours come from CSS tokens so both themes work. */

import { motion } from "motion/react";
import { scaleBand, scaleLinear, scalePoint, scaleSqrt, scaleTime } from "d3-scale";
import { line as d3line, curveMonotoneX } from "d3-shape";
import { max, min } from "d3-array";
import { createPortal } from "react-dom";
import { useCallback, useState, type ReactNode } from "react";
import { EASE, useDrawIn } from "./ui";
import { fmtDate, fmtNum, parseDate, useWidth } from "../util";

// ---- tooltip -------------------------------------------------------------------------------------

export function useTip() {
  const [tip, setTip] = useState<{ x: number; y: number; content: ReactNode } | null>(null);
  const show = useCallback((e: React.PointerEvent | React.FocusEvent, content: ReactNode) => {
    if ("clientX" in e) setTip({ x: e.clientX, y: e.clientY, content });
    else {
      const r = (e.target as Element).getBoundingClientRect();
      setTip({ x: r.right, y: r.top, content });
    }
  }, []);
  const hide = useCallback(() => setTip(null), []);
  const node = tip
    ? createPortal(
        <div
          className="chart-tip"
          style={{
            left: Math.min(tip.x + 14, window.innerWidth - 340),
            top: Math.min(tip.y + 14, window.innerHeight - 120),
          }}
        >
          {tip.content}
        </div>,
        document.body,
      )
    : null;
  return { show, hide, node };
}

const grow = (shown: boolean, instant: boolean, i: number, total = 1) => ({
  initial: { scaleX: 0 },
  animate: { scaleX: shown ? 1 : 0 },
  transition: instant ? { duration: 0 } : { duration: 0.62, ease: EASE, delay: Math.min(i * (0.3 / Math.max(total, 1)), 0.3) },
  style: { transformBox: "fill-box" as const, originX: 0 },
});

// ---- horizontal bars -----------------------------------------------------------------------------

export interface BarRow {
  label: string;
  value: number;
  color?: string;
  highlight?: boolean;
  valueLabel?: string;
  tip?: ReactNode;
}

export function HBars({ rows, unit = "", labelWidth = 150, rowH = 30 }: { rows: BarRow[]; unit?: string; labelWidth?: number; rowH?: number }) {
  const [wref, width] = useWidth<HTMLDivElement>();
  const { ref, shown, instant } = useDrawIn<SVGSVGElement>();
  const tip = useTip();
  const w = width || 280;
  const valueW = 70;
  const lw = Math.min(labelWidth, w * 0.36);
  const x = scaleLinear()
    .domain([0, max(rows, (r) => r.value) || 1])
    .range([0, w - lw - valueW - 8]);
  const h = rows.length * rowH;
  return (
    <div ref={wref}>
      <svg ref={ref} className="chart" width={w} height={h} role="img" aria-label="Bar chart">
        {rows.map((r, i) => {
          const y = i * rowH;
          const fill = r.color ?? (r.highlight ? "var(--rust)" : "var(--steel-2)");
          return (
            <g
              key={r.label}
              transform={`translate(0,${y})`}
              onPointerMove={(e) => r.tip && tip.show(e, r.tip)}
              onPointerLeave={tip.hide}
            >
              <text x={lw - 10} y={rowH / 2} dy="0.35em" textAnchor="end" className={r.highlight ? "lbl-strong" : "lbl"}>
                {r.label.length > 26 ? `${r.label.slice(0, 25)}…` : r.label}
              </text>
              <motion.rect x={lw} y={rowH * 0.2} height={rowH * 0.6} width={Math.max(0, x(r.value))} fill={fill} {...grow(shown, instant, i, rows.length)} />
              <motion.text
                x={lw + x(r.value) + 6}
                y={rowH / 2}
                dy="0.35em"
                className={r.highlight ? "lbl-strong" : undefined}
                initial={{ opacity: 0 }}
                animate={{ opacity: shown ? 1 : 0 }}
                transition={{ duration: instant ? 0 : 0.3, delay: instant ? 0 : 0.45 }}
              >
                {r.valueLabel ?? `${fmtNum(r.value)}${unit}`}
              </motion.text>
            </g>
          );
        })}
        <line x1={lw} x2={lw} y1={0} y2={h} className="base" />
      </svg>
      {tip.node}
    </div>
  );
}

/** Right-aligned SVG label that wraps onto a second line instead of truncating. */
function WrapLabel({ x, y, text, max, strong }: { x: number; y: number; text: string; max: number; strong?: boolean }) {
  const words = text.split(" ");
  const lines: string[] = [""];
  for (const w of words) {
    const cur = lines[lines.length - 1];
    if ((cur + " " + w).trim().length > max && cur) {
      if (lines.length === 2) {
        lines[1] = `${lines[1]} ${w}`;
        continue;
      }
      lines.push(w);
    } else lines[lines.length - 1] = (cur + " " + w).trim();
  }
  if (lines[1] && lines[1].length > max) lines[1] = `${lines[1].slice(0, max - 1)}…`;
  const cls = strong ? "lbl-strong" : "lbl";
  return lines.length === 1 ? (
    <text x={x} y={y} dy="0.35em" textAnchor="end" className={cls}>
      {lines[0]}
    </text>
  ) : (
    <text x={x} y={y} textAnchor="end" className={cls}>
      <tspan x={x} dy="-0.25em">
        {lines[0]}
      </tspan>
      <tspan x={x} dy="1.15em">
        {lines[1]}
      </tspan>
    </text>
  );
}

// ---- paired bars (previous vs current window) ----------------------------------------------------

export interface PairRow {
  label: string;
  prev: number;
  cur: number;
  highlight?: boolean;
  tip?: ReactNode;
}

export function PairedBars({ rows, labelWidth = 220 }: { rows: PairRow[]; labelWidth?: number }) {
  const [wref, width] = useWidth<HTMLDivElement>();
  const { ref, shown, instant } = useDrawIn<SVGSVGElement>();
  const tip = useTip();
  const w = width || 300;
  const lw = Math.min(labelWidth, w * 0.42);
  const rowH = 40;
  const x = scaleLinear()
    .domain([0, max(rows, (r) => Math.max(r.prev, r.cur)) || 1])
    .range([0, w - lw - 60]);
  return (
    <div ref={wref}>
      <div className="legend">
        <span>
          <i className="swatch" style={{ background: "var(--context)" }} /> Previous 7 days
        </span>
        <span>
          <i className="swatch" style={{ background: "var(--steel)" }} /> Current 7 days
        </span>
        <span>
          <i className="swatch" style={{ background: "var(--rust)" }} /> Emerging theme
        </span>
      </div>
      <svg ref={ref} className="chart" width={w} height={rows.length * rowH} role="img" aria-label="Theme volume">
        {rows.map((r, i) => (
          <g key={r.label} transform={`translate(0,${i * rowH})`} onPointerMove={(e) => r.tip && tip.show(e, r.tip)} onPointerLeave={tip.hide}>
            <WrapLabel x={lw - 10} y={rowH / 2} text={r.label} max={Math.floor(lw / 6.4)} strong={r.highlight} />
            <motion.rect x={lw} y={7} height={9} width={x(r.prev)} fill="var(--context)" {...grow(shown, instant, i, rows.length)} />
            <motion.rect
              x={lw}
              y={18}
              height={11}
              width={x(r.cur)}
              fill={r.highlight ? "var(--rust)" : "var(--steel)"}
              {...grow(shown, instant, i + 1, rows.length)}
            />
            <text x={lw + x(r.cur) + 6} y={23.5} dy="0.3em" className="lbl">
              {r.cur}
            </text>
          </g>
        ))}
        <line x1={lw} x2={lw} y1={0} y2={rows.length * rowH} className="base" />
      </svg>
      {tip.node}
    </div>
  );
}

// ---- lines with direct end labels ----------------------------------------------------------------

export interface Series {
  name: string;
  color: string;
  points: { x: string; y: number }[];
  emphasis?: boolean;
}

export function Lines({
  series,
  xs,
  height = 300,
  yFormat = (n: number) => fmtNum(n, 0),
  yDomain,
  labelW = 130,
  xFormat = (s: string) => s,
  valueFormat,
}: {
  series: Series[];
  xs: string[];
  height?: number;
  yFormat?: (n: number) => string;
  yDomain?: [number, number];
  labelW?: number;
  xFormat?: (s: string) => string;
  valueFormat?: (n: number) => string;
}) {
  const anyEmphasis = series.some((s) => s.emphasis);
  const [wref, width] = useWidth<HTMLDivElement>();
  const { ref, shown, instant } = useDrawIn<SVGSVGElement>();
  const tip = useTip();
  const w = width || 320;
  const m = { l: 48, r: Math.min(labelW, w * 0.3), t: 10, b: 28 };
  const all = series.flatMap((s) => s.points.map((p) => p.y));
  const x = scalePoint<string>().domain(xs).range([m.l, w - m.r]).padding(0.1);
  const y = scaleLinear()
    .domain(yDomain ?? [Math.min(0, min(all) ?? 0), (max(all) ?? 1) * 1.08])
    .nice()
    .range([height - m.b, m.t]);
  const gen = d3line<{ x: string; y: number }>()
    .x((p) => x(p.x) ?? 0)
    .y((p) => y(p.y))
    .curve(curveMonotoneX);
  // End labels, nudged apart so they never overlap.
  const ends = series
    .map((s) => ({ s, last: s.points[s.points.length - 1] }))
    .filter((e) => e.last)
    .map((e) => ({ ...e, ty: y(e.last.y) }))
    .sort((a, b) => a.ty - b.ty);
  for (let i = 1; i < ends.length; i++) if (ends[i].ty - ends[i - 1].ty < 14) ends[i].ty = ends[i - 1].ty + 14;
  const tickEvery = Math.ceil(xs.length / Math.max(2, Math.floor((w - m.l - m.r) / 70)));
  const [hover, setHover] = useState<string | null>(null);
  return (
    <div ref={wref}>
      <svg
        ref={ref}
        className="chart"
        width={w}
        height={height}
        role="img"
        aria-label="Line chart"
        onPointerLeave={() => {
          setHover(null);
          tip.hide();
        }}
      >
        <g className="grid">
          {y.ticks(5).map((t) => (
            <g key={t}>
              <line x1={m.l} x2={w - m.r} y1={y(t)} y2={y(t)} />
              <text x={m.l - 8} y={y(t)} dy="0.32em" textAnchor="end">
                {yFormat(t)}
              </text>
            </g>
          ))}
        </g>
        {xs.map((p, i) =>
          i % tickEvery === 0 || i === xs.length - 1 ? (
            <text key={p} x={x(p)} y={height - 8} textAnchor="middle">
              {xFormat(p)}
            </text>
          ) : null,
        )}
        {hover ? <line x1={x(hover)} x2={x(hover)} y1={m.t} y2={height - m.b} stroke="var(--rule)" /> : null}
        {series.map((s, i) => (
          <motion.path
            key={s.name}
            d={gen(s.points) ?? ""}
            fill="none"
            stroke={s.color}
            strokeWidth={s.emphasis ? 2.8 : 1.6}
            strokeOpacity={anyEmphasis && !s.emphasis ? 0.45 : 1}
            strokeLinejoin="round"
            strokeLinecap="round"
            initial={{ pathLength: 0 }}
            animate={{ pathLength: shown ? 1 : 0 }}
            transition={instant ? { duration: 0 } : { duration: 0.8, ease: EASE, delay: i * 0.04 }}
          />
        ))}
        {series.map((s) =>
          s.points.map((p) => (
            <motion.circle
              key={`${s.name}-${p.x}`}
              cx={x(p.x)}
              cy={y(p.y)}
              r={hover === p.x ? 4 : 2.4}
              fill="var(--paper)"
              stroke={s.color}
              strokeWidth={1.6}
              initial={{ opacity: 0 }}
              animate={{ opacity: shown ? 1 : 0 }}
              transition={{ duration: instant ? 0 : 0.3, delay: instant ? 0 : 0.6 }}
            />
          )),
        )}
        {ends.map((e) => (
          <motion.g
            key={e.s.name}
            initial={{ opacity: 0, x: -4 }}
            animate={{ opacity: shown ? 1 : 0, x: shown ? 0 : -4 }}
            transition={{ duration: instant ? 0 : 0.35, delay: instant ? 0 : 0.7 }}
          >
            <text x={w - m.r + 10} y={e.ty} dy="0.32em" className={e.s.emphasis ? "lbl-strong" : "lbl"} style={{ fill: e.s.color }}>
              {e.s.name.length > 16 ? `${e.s.name.slice(0, 15)}…` : e.s.name}
              {valueFormat ? <tspan style={{ fill: "var(--ink-3)", fontWeight: 500 }}> {valueFormat(e.last.y)}</tspan> : null}
            </text>
          </motion.g>
        ))}
        {xs.map((p) => (
          <rect
            key={`hit-${p}`}
            x={(x(p) ?? 0) - x.step() / 2}
            y={m.t}
            width={x.step()}
            height={height - m.t - m.b}
            fill="transparent"
            onPointerMove={(ev) => {
              setHover(p);
              const rows = series
                .map((s) => ({ s, v: s.points.find((q) => q.x === p)?.y }))
                .filter((r) => r.v !== undefined)
                .sort((a, b) => (b.v ?? 0) - (a.v ?? 0));
              tip.show(
                ev,
                <div>
                  <b>{xFormat(p)}</b>
                  {rows.map((r) => (
                    <div key={r.s.name} style={{ display: "flex", justifyContent: "space-between", gap: 16 }}>
                      <span>
                        <i className="swatch" style={{ background: r.s.color, marginRight: 6 }} />
                        {r.s.name}
                      </span>
                      <b className="tabular">{yFormat(r.v ?? 0)}</b>
                    </div>
                  ))}
                </div>,
              );
            }}
          />
        ))}
      </svg>
      {tip.node}
    </div>
  );
}

// ---- change timeline: one row per competitor -----------------------------------------------------

export interface Dot {
  id: number;
  row: string;
  date: string;
  size: number;
  color: string;
  kind: string;
  tip: ReactNode;
}

const KIND_SHAPE: Record<string, (r: number) => string> = {
  price: (r) => `M ${-r} 0 A ${r} ${r} 0 1 0 ${r} 0 A ${r} ${r} 0 1 0 ${-r} 0`,
  feature: (r) => `M ${-r * 0.9} ${-r * 0.9} h ${r * 1.8} v ${r * 1.8} h ${-r * 1.8} Z`,
  launch: (r) => `M 0 ${-r * 1.1} L ${r} ${r * 0.8} L ${-r} ${r * 0.8} Z`,
  policy: (r) => `M 0 ${-r * 1.15} L ${r * 1.15} 0 L 0 ${r * 1.15} L ${-r * 1.15} 0 Z`,
  dealer: (r) => `M ${-r} ${-r * 0.35} h ${r * 2} v ${r * 0.7} h ${-r * 2} Z M ${-r * 0.35} ${-r} h ${r * 0.7} v ${r * 2} h ${-r * 0.7} Z`,
  other: (r) => `M ${-r * 0.8} 0 A ${r * 0.8} ${r * 0.8} 0 1 0 ${r * 0.8} 0 A ${r * 0.8} ${r * 0.8} 0 1 0 ${-r * 0.8} 0`,
};

export function KindGlyph({ kind, color = "currentColor" }: { kind: string; color?: string }) {
  return (
    <svg width={12} height={12} viewBox="-7 -7 14 14" aria-hidden style={{ flex: "none" }}>
      <path d={(KIND_SHAPE[kind] ?? KIND_SHAPE.other)(5)} fill={kind === "other" ? "none" : color} stroke={color} strokeWidth={1.4} />
    </svg>
  );
}

export function Timeline({ rows, dots, from, to, onPick }: { rows: string[]; dots: Dot[]; from: Date; to: Date; onPick?: (id: number) => void }) {
  const [wref, width] = useWidth<HTMLDivElement>();
  const { ref, shown, instant } = useDrawIn<SVGSVGElement>();
  const tip = useTip();
  const w = width || 320;
  const lw = Math.min(140, w * 0.3);
  const rowH = 34;
  const h = rows.length * rowH + 26;
  const pad = (to.getTime() - from.getTime()) * 0.04;
  const x = scaleTime()
    .domain([from, new Date(to.getTime() + pad)])
    .range([lw + 8, w - 12]);
  const y = scaleBand<string>().domain(rows).range([0, rows.length * rowH]);
  const r = scaleSqrt().domain([0, 1]).range([2.5, 8]);
  const ticks = x.ticks(Math.max(2, Math.floor((w - lw) / 110))).filter((t) => Math.abs(x(t) - x(to)) > 56 && t <= to);
  const span = to.getTime() - from.getTime() || 1;
  return (
    <div ref={wref}>
      <svg ref={ref} className="chart" width={w} height={h} role="img" aria-label="Change timeline by competitor">
        <g className="grid">
          {ticks.map((t) => (
            <g key={t.toISOString()}>
              <line x1={x(t)} x2={x(t)} y1={0} y2={rows.length * rowH} />
              <text x={x(t)} y={h - 6} textAnchor="middle">
                {fmtDate(t.toISOString(), false)}
              </text>
            </g>
          ))}
        </g>
        <line x1={x(to)} x2={x(to)} y1={-4} y2={rows.length * rowH} stroke="var(--ink-3)" strokeDasharray="2 3" />
        <text x={x(to)} y={h - 6} textAnchor="middle" className="lbl-strong">
          As of {fmtDate(to.toISOString(), false)}
        </text>
        {rows.map((row) => (
          <g key={row} transform={`translate(0,${(y(row) ?? 0) + rowH / 2})`}>
            <line x1={lw + 8} x2={w - 12} y1={0} y2={0} stroke="var(--rule)" strokeDasharray="1 3" />
            <text x={lw - 4} y={0} dy="0.35em" textAnchor="end" className="lbl">
              {row.length > 20 ? `${row.slice(0, 19)}…` : row}
            </text>
          </g>
        ))}
        {dots.map((d) => {
          const dt = parseDate(d.date) ?? from;
          const cx = x(dt);
          const cy = (y(d.row) ?? 0) + rowH / 2;
          const delay = instant ? 0 : ((dt.getTime() - from.getTime()) / span) * 0.45;
          return (
            <motion.g
              key={d.id}
              transform={`translate(${cx},${cy})`}
              initial={{ opacity: 0 }}
              animate={{ opacity: shown ? 1 : 0 }}
              transition={{ duration: instant ? 0 : 0.35, delay }}
              style={{ cursor: onPick ? "pointer" : undefined }}
              onPointerMove={(e) => tip.show(e, d.tip)}
              onPointerLeave={tip.hide}
              onClick={() => onPick?.(d.id)}
              tabIndex={onPick ? 0 : undefined}
              onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onPick?.(d.id)}
              onFocus={(e) => tip.show(e, d.tip)}
              onBlur={tip.hide}
            >
              <motion.path
                d={(KIND_SHAPE[d.kind] ?? KIND_SHAPE.other)(r(d.size))}
                fill={d.kind === "other" ? "var(--paper)" : d.color}
                fillOpacity={0.9}
                stroke={d.kind === "other" ? d.color : "var(--paper)"}
                strokeWidth={d.kind === "other" ? 2 : 1}
                initial={{ scale: 0 }}
                animate={{ scale: shown ? 1 : 0 }}
                transition={instant ? { duration: 0 } : { type: "spring", stiffness: 420, damping: 22, delay }}
                whileHover={{ scale: 1.35 }}
              />
            </motion.g>
          );
        })}
      </svg>
      {tip.node}
    </div>
  );
}

// ---- heatmap (diverging) -------------------------------------------------------------------------

export function Heatmap({
  rows,
  cols,
  cell,
  rowFormat = (s: string) => s,
}: {
  rowFormat?: (s: string) => string;
  rows: string[];
  cols: string[];
  cell: (r: string, c: string) => { v: number; n: number } | undefined;
}) {
  const [wref, width] = useWidth<HTMLDivElement>();
  const { ref, shown, instant } = useDrawIn<SVGSVGElement>();
  const tip = useTip();
  const w = width || 320;
  const lw = Math.min(150, w * 0.3);
  const headH = 58;
  const cw = Math.max(38, (w - lw) / Math.max(cols.length, 1));
  const ch = 32;
  const totalW = lw + cw * cols.length;
  const fillFor = (v: number) => {
    const p = Math.round(Math.min(1, Math.abs(v)) * 100);
    const end = v >= 0 ? "var(--div-pos)" : "var(--div-neg)";
    return `color-mix(in oklab, ${end} ${p}%, var(--div-mid))`;
  };
  return (
    <div ref={wref} style={{ overflowX: "auto" }}>
      <svg ref={ref} className="chart" width={totalW} height={headH + rows.length * ch} role="img" aria-label="Sentiment by aspect and brand">
        {cols.map((c, j) => (
          <text key={c} transform={`translate(${lw + j * cw + cw / 2},${headH - 8}) rotate(-35)`} className="lbl">
            {c.length > 14 ? `${c.slice(0, 13)}…` : c}
          </text>
        ))}
        {rows.map((r, i) => (
          <g key={r} transform={`translate(0,${headH + i * ch})`}>
            <text x={lw - 10} y={ch / 2} dy="0.35em" textAnchor="end" className="lbl">
              {rowFormat(r)}
            </text>
            {cols.map((c, j) => {
              const d = cell(r, c);
              const delay = instant ? 0 : Math.min((i + j) * 0.025, 0.35);
              return (
                <g
                  key={c}
                  onPointerMove={(e) =>
                    d &&
                    tip.show(
                      e,
                      <div>
                        <b>
                          {rowFormat(r)} · {c}
                        </b>
                        <div>
                          Mean sentiment <b className="tabular">{d.v >= 0 ? "+" : "−"}{Math.abs(d.v).toFixed(2)}</b> from {d.n} post{d.n === 1 ? "" : "s"}
                        </div>
                      </div>,
                    )
                  }
                  onPointerLeave={tip.hide}
                >
                  <motion.rect
                    x={lw + j * cw + 1}
                    y={1}
                    width={cw - 2}
                    height={ch - 2}
                    fill={d ? fillFor(d.v) : "transparent"}
                    stroke={d ? "none" : "var(--rule-soft)"}
                    initial={{ opacity: 0 }}
                    animate={{ opacity: shown ? 1 : 0 }}
                    transition={{ duration: instant ? 0 : 0.35, delay }}
                  />
                  {d ? (
                    <text
                      x={lw + j * cw + cw / 2}
                      y={ch / 2}
                      dy="0.35em"
                      textAnchor="middle"
                      style={{ fill: Math.abs(d.v) > 0.55 ? "var(--paper)" : "var(--ink)", fontSize: 11 }}
                    >
                      {d.v >= 0 ? "+" : "−"}
                      {Math.abs(d.v).toFixed(2)}
                    </text>
                  ) : null}
                </g>
              );
            })}
          </g>
        ))}
      </svg>
      {tip.node}
    </div>
  );
}

// ---- claimed vs reported: IQR bar, median dot, claimed ring ---------------------------------------

export interface RangeRow {
  label: string;
  q1: number;
  q3: number;
  median: number;
  claimed: number | null;
  low: boolean;
  tip?: ReactNode;
}

export function RangeDots({ rows, unit }: { rows: RangeRow[]; unit: string }) {
  const [wref, width] = useWidth<HTMLDivElement>();
  const { ref, shown, instant } = useDrawIn<SVGSVGElement>();
  const tip = useTip();
  const w = width || 320;
  const lw = Math.min(210, w * 0.4);
  const rowH = 40;
  const vals = rows.flatMap((r) => [r.q1, r.q3, r.median, r.claimed ?? r.median]);
  const x = scaleLinear()
    .domain([Math.min(...vals) * 0.9, Math.max(...vals) * 1.05])
    .nice()
    .range([lw + 10, w - 16]);
  const h = rows.length * rowH + 26;
  return (
    <div ref={wref}>
      <div className="legend">
        <span>
          <svg width={26} height={10}>
            <line x1={0} x2={26} y1={5} y2={5} stroke="var(--steel)" strokeWidth={4} />
          </svg>
          Owner-reported middle half (IQR)
        </span>
        <span>
          <svg width={10} height={10}>
            <circle cx={5} cy={5} r={4} fill="var(--steel)" />
          </svg>
          Reported median
        </span>
        <span>
          <svg width={12} height={12}>
            <circle cx={6} cy={6} r={4.5} fill="none" stroke="var(--rust)" strokeWidth={2} />
          </svg>
          Brand claim
        </span>
      </div>
      <svg ref={ref} className="chart" width={w} height={h} role="img" aria-label="Claimed versus owner-reported">
        <g className="grid">
          {x.ticks(6).map((t) => (
            <g key={t}>
              <line x1={x(t)} x2={x(t)} y1={0} y2={rows.length * rowH} />
              <text x={x(t)} y={h - 6} textAnchor="middle">
                {fmtNum(t, 0)} {unit}
              </text>
            </g>
          ))}
        </g>
        {rows.map((r, i) => {
          const color = r.low ? "var(--context)" : "var(--steel)";
          const cy = i * rowH + rowH / 2;
          return (
            <g key={r.label} onPointerMove={(e) => r.tip && tip.show(e, r.tip)} onPointerLeave={tip.hide}>
              <text x={lw - 10} y={cy} dy="0.35em" textAnchor="end" className="lbl">
                {r.label}
              </text>
              <motion.line
                x1={x(r.q1)}
                x2={x(r.q3)}
                y1={cy}
                y2={cy}
                stroke={color}
                strokeWidth={5}
                strokeLinecap="butt"
                initial={{ pathLength: 0 }}
                animate={{ pathLength: shown ? 1 : 0 }}
                transition={instant ? { duration: 0 } : { duration: 0.55, ease: EASE, delay: i * 0.05 }}
              />
              <motion.circle
                cx={x(r.median)}
                cy={cy}
                r={5.5}
                fill={color}
                stroke="var(--paper)"
                strokeWidth={1.5}
                initial={{ scale: 0 }}
                animate={{ scale: shown ? 1 : 0 }}
                transition={instant ? { duration: 0 } : { type: "spring", stiffness: 400, damping: 24, delay: 0.35 + i * 0.05 }}
              />
              {r.claimed !== null ? (
                <motion.circle
                  cx={x(r.claimed)}
                  cy={cy}
                  r={6}
                  fill="none"
                  stroke="var(--rust)"
                  strokeWidth={2}
                  initial={{ opacity: 0, scale: 1.8 }}
                  animate={{ opacity: shown ? 1 : 0, scale: shown ? 1 : 1.8 }}
                  transition={instant ? { duration: 0 } : { duration: 0.4, ease: EASE, delay: 0.55 + i * 0.05 }}
                />
              ) : null}
            </g>
          );
        })}
      </svg>
      {tip.node}
    </div>
  );
}

// ---- two-part stacked bars (caught vs missed) ----------------------------------------------------

export function SplitBars({ rows }: { rows: { label: string; a: number; b: number }[] }) {
  const [wref, width] = useWidth<HTMLDivElement>();
  const { ref, shown, instant } = useDrawIn<SVGSVGElement>();
  const w = width || 300;
  const lw = Math.min(190, w * 0.38);
  const rowH = 30;
  const x = scaleLinear()
    .domain([0, max(rows, (r) => r.a + r.b) || 1])
    .range([0, w - lw - 70]);
  return (
    <div ref={wref}>
      <div className="legend">
        <span>
          <i className="swatch" style={{ background: "var(--verified)" }} /> Caught by the critic
        </span>
        <span>
          <i className="swatch" style={{ background: "var(--rust)" }} /> Missed
        </span>
      </div>
      <svg ref={ref} className="chart" width={w} height={rows.length * rowH} role="img" aria-label="Corruptions caught by type">
        {rows.map((r, i) => (
          <g key={r.label} transform={`translate(0,${i * rowH})`}>
            <text x={lw - 10} y={rowH / 2} dy="0.35em" textAnchor="end" className="lbl">
              {r.label.replace(/_/g, " ")}
            </text>
            <motion.rect x={lw} y={7} height={16} width={x(r.a)} fill="var(--verified)" {...grow(shown, instant, i, rows.length)} />
            {r.b ? <motion.rect x={lw + x(r.a)} y={7} height={16} width={x(r.b)} fill="var(--rust)" {...grow(shown, instant, i + 2, rows.length)} /> : null}
            <text x={lw + x(r.a + r.b) + 6} y={rowH / 2} dy="0.35em" className="lbl">
              {r.a}/{r.a + r.b}
            </text>
          </g>
        ))}
        <line x1={lw} x2={lw} y1={0} y2={rows.length * rowH} className="base" />
      </svg>
    </div>
  );
}
