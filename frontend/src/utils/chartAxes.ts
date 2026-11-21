// Axis steps for the hand-drawn charts (docs/design-system/components/TrendChart).

// One decimal place is finer than a pixel and keeps path text short.
export const fix = (n: number) => n.toFixed(1);
export const tickText = (v: number) => String(Number(v.toFixed(6)));

export function valueTicks(min: number, max: number, count = 4): number[] {
  if (min === max) {
    min -= 1;
    max += 1;
  }
  const raw = (max - min) / Math.max(count - 1, 1);
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= raw) ?? 10 * magnitude;
  const first = Math.floor(min / step) * step;
  const last = Math.ceil(max / step) * step;
  const ticks: number[] = [];
  for (let v = first; v <= last + step / 1000; v += step) ticks.push(Number(v.toFixed(10)));
  return ticks;
}

// Up to an hour for the simulation preview; hours and days for trend ranges.
const MINUTE_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 180, 360, 720, 1440, 2880, 10080];

export function timeTicks(start: number, end: number, count = 6): number[] {
  const spanMinutes = (end - start) / 60;
  const stepMinutes = MINUTE_STEPS.find((s) => spanMinutes / s <= count) ?? 10080;
  const step = stepMinutes * 60;
  const ticks: number[] = [];
  for (let t = Math.ceil(start / step) * step; t <= end; t += step) ticks.push(t);
  return ticks;
}

export function utcClock(t: number): string {
  return new Date(t * 1000).toISOString().slice(11, 16);
}

export function isUtcMidnight(t: number): boolean {
  return t % 86400 === 0;
}

export function utcDate(t: number): string {
  return new Date(t * 1000).toISOString().slice(5, 10);
}
