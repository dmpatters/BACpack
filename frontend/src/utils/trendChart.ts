// Drawing helpers for TrendChart (docs/design-system/components/TrendChart).
// Times are seconds since the epoch, UTC.
import type { TrendGapDTO, TrendRangeDTO } from "../api/types";
import { fix, isUtcMidnight, utcClock, utcDate } from "./chartAxes";

export interface ChartPoint {
  t: number;
  v: number;
}

export type PresetKey = "1h" | "6h" | "24h" | "7d" | "30d";

export const PRESETS: { key: PresetKey; label: string; seconds: number }[] = [
  { key: "1h", label: "1 h", seconds: 3600 },
  { key: "6h", label: "6 h", seconds: 6 * 3600 },
  { key: "24h", label: "24 h", seconds: 86400 },
  { key: "7d", label: "7 d", seconds: 7 * 86400 },
  { key: "30d", label: "30 d", seconds: 30 * 86400 },
];

export function maxPointsFor(widthPx: number): number {
  return Math.min(5000, Math.max(10, Math.round(widthPx / 2)));
}

export const seconds = (iso: string) => Date.parse(iso) / 1000;

// Clipped to the window drawn, so no point lands in the label margin.
export function samplesWithin(range: TrendRangeDTO, start: number, end: number): ChartPoint[] {
  return range.samples
    .map((s) => ({ t: seconds(s.timestamp), v: s.value }))
    .filter((p) => p.t >= start && p.t <= end);
}

// A step or COV line starts at the left edge with the value it had there; a
// COV trend holds its last value up to the end (a quiet stretch isn't a gap).
export function toPoints(range: TrendRangeDTO, start: number, end: number): ChartPoint[] {
  const points = samplesWithin(range, start, end);
  const holds = range.step || range.mode === "cov";
  if (holds && range.carry_in && (points.length === 0 || points[0].t > start)) {
    points.unshift({ t: start, v: range.carry_in.value });
  }
  if (range.mode === "cov" && points.length > 0 && points[points.length - 1].t < end) {
    points.push({ t: end, v: points[points.length - 1].v });
  }
  return points;
}

// Split wherever a gap lies between two neighbours, so the line never joins
// across it.
export function segments(points: ChartPoint[], gaps: TrendGapDTO[]): ChartPoint[][] {
  if (points.length === 0) return [];
  const spans = gaps.map((g) => ({ start: seconds(g.start), end: seconds(g.end) }));
  const out: ChartPoint[][] = [[points[0]]];
  for (let i = 1; i < points.length; i++) {
    const before = points[i - 1].t;
    const after = points[i].t;
    if (spans.some((g) => g.start >= before && g.end <= after && g.end > g.start)) out.push([]);
    out[out.length - 1].push(points[i]);
  }
  return out;
}

export function linePath(seg: ChartPoint[], x: (t: number) => number, y: (v: number) => number, step: boolean): string {
  if (seg.length === 0) return "";
  let d = `M${fix(x(seg[0].t))},${fix(y(seg[0].v))}`;
  for (let i = 1; i < seg.length; i++) {
    if (step) d += ` L${fix(x(seg[i].t))},${fix(y(seg[i - 1].v))}`;
    d += ` L${fix(x(seg[i].t))},${fix(y(seg[i].v))}`;
  }
  return d;
}

export function nearestIndex(points: ChartPoint[], t: number): number {
  let best = 0;
  for (let i = 1; i < points.length; i++) {
    if (Math.abs(points[i].t - t) < Math.abs(points[best].t - t)) best = i;
  }
  return best;
}

// A short gap (a device restart) would read "21:13–21:13" in minutes alone.
const SECONDS_BELOW = 600;
const clockWithSeconds = (t: number) => new Date(t * 1000).toISOString().slice(11, 19);

function gapSpan(gap: TrendGapDTO): string {
  const a = seconds(gap.start);
  const b = seconds(gap.end);
  const clock = b - a < SECONDS_BELOW ? clockWithSeconds : utcClock;
  const sameDay = Math.floor(a / 86400) === Math.floor(b / 86400) || (isUtcMidnight(b) && b - a <= 86400);
  return sameDay ? `${clock(a)}–${clock(b)}` : `${utcDate(a)} ${clock(a)}–${utcDate(b)} ${clock(b)}`;
}

export function gapText(gaps: TrendGapDTO[]): string {
  if (gaps.length === 0) return "";
  const named = gaps.slice(0, 3).map(gapSpan).join(", ");
  const more = gaps.length > 3 ? ` and ${gaps.length - 3} more` : "";
  return `no samples ${named}${more}`;
}
