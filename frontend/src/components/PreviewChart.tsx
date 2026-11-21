import type { PreviewSeries } from "../utils/simPreview";
import { fix, tickText, timeTicks, utcClock, valueTicks } from "../utils/chartAxes";
import { linePath } from "../utils/trendChart";

const W = 560;
const H = 238;
const X0 = 64;
const X1 = 492;
const Y0 = 20;
const Y1 = 200;

// The drawn shape of a preview, on the design system's chart classes. A step
// chart (binary, multi-state) places each named level in its own band.
export default function PreviewChart({
  series,
  label,
  levels,
  unit,
  format = String,
}: {
  series: PreviewSeries;
  label: string;
  levels?: { value: number; label: string }[];
  unit?: string;
  format?: (v: number) => string;
}) {
  const { points } = series;
  if (points.length === 0) return null;
  const values = points.map((p) => p.v);
  const ticks = levels ?? valueTicks(Math.min(...values), Math.max(...values)).map((v) => ({ value: v, label: tickText(v) }));
  const lo = Math.min(...ticks.map((t) => t.value));
  const hi = Math.max(...ticks.map((t) => t.value));
  const x = (t: number) => X0 + ((t - series.start) / (series.end - series.start || 1)) * (X1 - X0);
  const y = levels
    ? (v: number) => Y1 - ((levels.findIndex((l) => l.value === v) + 0.5) / levels.length) * (Y1 - Y0)
    : (v: number) => Y1 - ((v - lo) / (hi - lo || 1)) * (Y1 - Y0);

  const line = linePath(points, x, y, series.step);
  const lastX = fix(x(points[points.length - 1].t));
  const first = points[0];

  return (
    <svg className="bp-chart__svg" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={label}>
      {ticks.map((tick) => (
        <g key={tick.value}>
          <line className="bp-chart__grid" x1={X0} x2={X1} y1={fix(y(tick.value))} y2={fix(y(tick.value))} />
          <text className="bp-chart__text" x={X0 - 8} y={fix(y(tick.value) + 4)} textAnchor="end">
            {tick.label}
          </text>
        </g>
      ))}
      {!series.step && <path className="bp-chart__area" d={`${line} L${lastX},${Y1} L${fix(X0)},${Y1} Z`} />}
      <path className="bp-chart__line" d={line} />
      {timeTicks(series.start, series.end).map((t) => (
        <g key={t}>
          <line className="bp-chart__tick" x1={fix(x(t))} x2={fix(x(t))} y1={Y1} y2={Y1 + 4} />
          <text className="bp-chart__text" x={fix(x(t))} y={Y1 + 18} textAnchor="middle">
            {utcClock(t)}
          </text>
        </g>
      ))}
      <text className="bp-chart__text" x={X1} y={H - 4} textAnchor="end">
        time (UTC)
      </text>
      <circle className="bp-chart__dot" cx={fix(x(first.t))} cy={fix(y(first.v))} r={4} />
      {!levels && (
        <text className="bp-chart__label" x={fix(x(first.t) + 10)} y={fix(y(first.v) - 8)}>
          now {format(first.v)}
          {unit ? ` ${unit}` : ""}
        </text>
      )}
    </svg>
  );
}
