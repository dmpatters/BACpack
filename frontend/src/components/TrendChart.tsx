import { useEffect, useId, useRef, useState, type RefObject } from "react";
import type { TrendRangeDTO } from "../api/types";
import Button from "./Button";
import { TableFrame } from "./DataTable";
import { fix, isUtcMidnight, tickText, timeTicks, utcClock, utcDate, valueTicks } from "../utils/chartAxes";
import { gapText, linePath, nearestIndex, samplesWithin, segments, toPoints, type ChartPoint } from "../utils/trendChart";
import { formatUtc, formatUtcBare } from "../utils/time";

// Below this the plot scrolls inside its card rather than shrinking its text.
export const MIN_CHART_WIDTH = 560;
const H = 252;
const Y0 = 16;
const Y1 = 212;
const RIGHT = 72; // room for the latest value's label

const iso = (t: number) => new Date(t * 1000).toISOString();

// The element's width, kept current; jsdom (no ResizeObserver, no layout)
// gets the fallback.
export function useElementWidth(ref: RefObject<HTMLElement>, fallback = MIN_CHART_WIDTH): number {
  const [width, setWidth] = useState(fallback);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry.contentRect.width > 0) setWidth(entry.contentRect.width);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref]);
  return width;
}

interface Level {
  value: number;
  label: string;
}

// docs/design-system/components/TrendChart: one object, one unit, a table twin.
export default function TrendChart({
  range,
  start,
  end,
  title,
  sub,
  unit,
  levels,
  stale = false,
  format,
}: {
  range: TrendRangeDTO;
  start: number;
  end: number;
  title: string;
  sub: string;
  unit: string;
  levels?: Level[];
  stale?: boolean;
  format: (v: number) => string;
}) {
  const titleId = useId();
  const plotRef = useRef<HTMLDivElement>(null);
  const width = Math.max(useElementWidth(plotRef), MIN_CHART_WIDTH);
  const [asTable, setAsTable] = useState(false);
  const [active, setActive] = useState<number | null>(null);

  const samples: ChartPoint[] = samplesWithin(range, start, end);
  const points = toPoints(range, start, end);
  const shownLevels = levels ? withStrayValues(levels, points) : undefined;
  const valueText = (v: number) =>
    shownLevels ? (shownLevels.find((l) => l.value === v)?.label ?? String(v)) : `${format(v)}${unit ? ` ${unit}` : ""}`;

  const X0 = shownLevels ? 128 : 52;
  const X1 = width - RIGHT;
  const x = (t: number) => X0 + ((t - start) / (end - start || 1)) * (X1 - X0);
  const ticks = shownLevels
    ? shownLevels
    : valueTicks(...extent(points.map((p) => p.v))).map((v) => ({ value: v, label: tickText(v) }));
  const lo = Math.min(...ticks.map((t) => t.value));
  const hi = Math.max(...ticks.map((t) => t.value));
  const y = shownLevels
    ? (v: number) => Y1 - ((shownLevels.findIndex((l) => l.value === v) + 0.5) / shownLevels.length) * (Y1 - Y0)
    : (v: number) => Y1 - ((v - lo) / (hi - lo || 1)) * (Y1 - Y0);

  const step = range.step || range.mode === "cov";
  const segs = segments(points, range.gaps);
  const last = points[points.length - 1];
  const latest = samples[samples.length - 1];
  const tip = active !== null ? samples[active] : null;

  const onKey = (e: React.KeyboardEvent) => {
    if (samples.length === 0) return;
    const at = active ?? samples.length - 1;
    const next =
      e.key === "ArrowLeft" ? Math.max(0, at - 1)
      : e.key === "ArrowRight" ? Math.min(samples.length - 1, at + 1)
      : e.key === "Home" ? 0
      : e.key === "End" ? samples.length - 1
      : null;
    if (next === null) return;
    e.preventDefault();
    setActive(next);
  };
  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const box = e.currentTarget.getBoundingClientRect();
    if (box.width === 0 || samples.length === 0) return;
    const vx = ((e.clientX - box.left) / box.width) * width;
    setActive(nearestIndex(samples, start + ((vx - X0) / (X1 - X0 || 1)) * (end - start)));
  };

  const foot = footerText(range, samples.length, start);

  return (
    <section className={stale ? "bp-chart bp-chart--stale" : "bp-chart"} aria-labelledby={titleId}>
      <div className="bp-chart__head">
        <h3 className="bp-chart__title" id={titleId}>
          {title}
          <span className="bp-chart__sub">{sub}</span>
        </h3>
        <Button variant="ghost" small type="button" onClick={() => setAsTable((t) => !t)}>
          {asTable ? "View as chart" : "View as table"}
        </Button>
      </div>
      {asTable ? (
        <TableFrame label="Trend samples" scroll>
          <thead>
            <tr>
              <th>Time (UTC)</th>
              <th className="bp-cell-num">Value</th>
            </tr>
          </thead>
          <tbody>
            {[...range.samples].reverse().map((s, i) => (
              <tr key={i}>
                <td className="bp-cell-mono">{formatUtcBare(s.timestamp)}</td>
                <td className="bp-cell-num">{valueText(s.value)}</td>
              </tr>
            ))}
          </tbody>
        </TableFrame>
      ) : (
        <div
          className="bp-chart__plot"
          ref={plotRef}
          tabIndex={points.length > 0 ? 0 : undefined}
          aria-label={points.length > 0 ? "Samples; arrow keys move between them" : undefined}
          onKeyDown={onKey}
          onFocus={() => setActive((a) => a ?? (samples.length > 0 ? samples.length - 1 : null))}
          onBlur={() => setActive(null)}
        >
          {points.length === 0 ? (
            <p className="bp-help">No samples in this range.</p>
          ) : (
            <div className="bp-chart__inner">
              <svg
                className="bp-chart__svg"
                viewBox={`0 0 ${fix(width)} ${H}`}
                width={fix(width)}
                role="img"
                aria-label={`${title}, ${formatUtc(iso(start))} to ${formatUtc(iso(end))}${
                  latest ? `, latest ${valueText(latest.v)}` : ""
                }`}
                onMouseMove={onMove}
                onMouseLeave={() => setActive(null)}
              >
                {ticks.map((tick) => (
                  <g key={tick.value}>
                    <line className="bp-chart__grid" x1={X0} x2={fix(X1)} y1={fix(y(tick.value))} y2={fix(y(tick.value))} />
                    <text className="bp-chart__text" x={X0 - 8} y={fix(y(tick.value) + 4)} textAnchor="end">
                      {tick.label}
                    </text>
                  </g>
                ))}
                {!step &&
                  segs.map((seg, i) => (
                    <path
                      key={`a${i}`}
                      className="bp-chart__area"
                      d={`${linePath(seg, x, y, false)} L${fix(x(seg[seg.length - 1].t))},${Y1} L${fix(x(seg[0].t))},${Y1} Z`}
                    />
                  ))}
                {segs.map((seg, i) => (
                  <path key={`l${i}`} className="bp-chart__line" d={linePath(seg, x, y, step)} />
                ))}
                {timeTicks(start, end).map((t) => (
                  <g key={t}>
                    <line className="bp-chart__tick" x1={fix(x(t))} x2={fix(x(t))} y1={Y1} y2={Y1 + 4} />
                    <text className="bp-chart__text" x={fix(x(t))} y={Y1 + 18} textAnchor="middle">
                      {utcClock(t)}
                    </text>
                    {isUtcMidnight(t) && (
                      <text className="bp-chart__text bp-chart__text--date" x={fix(x(t))} y={Y1 + 32} textAnchor="middle">
                        {utcDate(t)}
                      </text>
                    )}
                  </g>
                ))}
                {tip && <line className="bp-chart__cross" x1={fix(x(tip.t))} x2={fix(x(tip.t))} y1={Y0} y2={Y1} />}
                <circle className="bp-chart__dot" cx={fix(x(last.t))} cy={fix(y(last.v))} r={4} />
                <text className="bp-chart__label" x={fix(x(last.t) + 10)} y={fix(y(last.v) + 4)}>
                  {valueText(last.v)}
                </text>
              </svg>
              {tip && (
                <div
                  className="bp-tip"
                  role="tooltip"
                  // A position on the plot, not a design value.
                  style={{ left: `calc(${fix((x(tip.t) / width) * 100)}% + 12px)`, top: `${fix(y(tip.v))}px` }}
                >
                  <span className="bp-tip__value">
                    <span className="bp-tip__key" />
                    {valueText(tip.v)}
                  </span>
                  <span className="bp-tip__time">{formatUtc(iso(tip.t))}</span>
                </div>
              )}
            </div>
          )}
        </div>
      )}
      <div className="bp-chart__foot">
        <span>{foot}</span>
        {latest && <span>Latest {formatUtc(iso(latest.t))}</span>}
      </div>
    </section>
  );
}

function extent(values: number[]): [number, number] {
  return values.length === 0 ? [0, 1] : [Math.min(...values), Math.max(...values)];
}

// A value the object's states don't name still gets a band of its own, so it
// is neither dropped nor drawn on another state's line.
function withStrayValues(levels: Level[], points: ChartPoint[]): Level[] {
  const known = new Set(levels.map((l) => l.value));
  const strays = [...new Set(points.map((p) => p.v))].filter((v) => !known.has(v)).sort((a, b) => a - b);
  return [...levels, ...strays.map((v) => ({ value: v, label: `${v} · not a state` }))];
}

function footerText(range: TrendRangeDTO, shown: number, start: number): string {
  const n = range.count_in_range;
  const parts = [
    `${n} ${n === 1 ? "sample" : "samples"} · ${range.mode === "cov" ? "change of value" : `every ${range.interval_seconds} s`}`,
  ];
  if (range.mode === "cov") parts.push("Change-of-value trend");
  const gaps = gapText(range.gaps);
  if (gaps) parts.push(gaps);
  if (range.thinned) {
    parts.push(`showing ${shown} of ${n}; export CSV for all`);
    if (range.step) parts.push("fast changes may be hidden");
  }
  if (range.earliest_sample && Date.parse(range.earliest_sample) / 1000 > start) {
    parts.push(`history starts ${formatUtc(range.earliest_sample)}`);
  }
  return parts.join(" · ");
}
