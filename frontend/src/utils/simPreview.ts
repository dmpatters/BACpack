import type { Simulation } from "../api/types";
import { PERIODIC_KINDS, type SimFamily } from "./simulation";

// The value simulator's own formulas (src/bacpack/simulation.py), run in the
// browser so the preview shows what the device will do before a save. The
// simulator runs on host time and the preview on the browser's clock; a host
// whose clock is off moves the shape, not its form. Python rounds halves to
// even, so a preview can differ from the device in the last decimal.

export interface PreviewPoint {
  t: number;
  v: number;
}

export interface PreviewSeries {
  step: boolean;
  random: boolean;
  start: number;
  end: number;
  points: PreviewPoint[];
}

const MIN_WINDOW = 300;
const MAX_WINDOW = 1200;

// A fixed seed, so an example run doesn't change on every keystroke.
export function seededRandom(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const num = (sim: Simulation, key: string, fallback: number): number =>
  typeof sim[key] === "number" && Number.isFinite(sim[key]) ? (sim[key] as number) : fallback;

export function statesOf(sim: Simulation, stateCount: number): number[] {
  const chosen = Array.isArray(sim.states) && sim.states.length ? (sim.states as number[]) : null;
  return chosen ? chosen.filter((s) => s <= stateCount) : Array.from({ length: stateCount }, (_, i) => i + 1);
}

export function previewWindow(sim: Simulation, stateCount: number): number {
  if (!PERIODIC_KINDS.includes(sim.kind)) return MAX_WINDOW;
  const cycle =
    sim.kind === "cycle"
      ? num(sim, "dwell_s", 60) * Math.max(statesOf(sim, stateCount).length, 1)
      : num(sim, "period_s", 600);
  return Math.min(MAX_WINDOW, Math.max(MIN_WINDOW, 3 * cycle));
}

function position(sim: Simulation, t: number): number {
  const period = num(sim, "period_s", 600);
  return ((t + num(sim, "phase_s", 0)) % period) / period;
}

function finish(sim: Simulation, value: number): number {
  if (typeof sim.min === "number") value = Math.max(value, sim.min);
  if (typeof sim.max === "number") value = Math.min(value, sim.max);
  const scale = 10 ** num(sim, "decimals", 2);
  return Math.round(value * scale) / scale;
}

function analogSeries(sim: Simulation, start: number, end: number, random: () => number): PreviewPoint[] {
  const low = num(sim, "low", 0);
  const high = num(sim, "high", 100);
  const step = Math.max(num(sim, "interval_s", 5), (end - start) / 400);
  let walk = num(sim, "start", 50);
  const points: PreviewPoint[] = [];
  for (let t = start; t <= end; t += step) {
    let v: number;
    switch (sim.kind) {
      case "sine":
        v = num(sim, "center", 50) + num(sim, "amplitude", 10) * Math.sin(2 * Math.PI * position(sim, t));
        break;
      case "triangle": {
        const p = position(sim, t);
        v = low + (high - low) * (p < 0.5 ? 2 * p : 2 * (1 - p));
        break;
      }
      case "sawtooth":
        v = low + (high - low) * position(sim, t);
        break;
      case "square":
        v = position(sim, t) < num(sim, "duty", 0.5) ? high : low;
        break;
      case "random_walk": {
        let next = walk + (random() * 2 - 1) * num(sim, "max_step", 1);
        if (next > high) next = 2 * high - next;
        if (next < low) next = 2 * low - next;
        walk = Math.min(Math.max(next, low), high);
        v = walk;
        break;
      }
      default: // noise
        v = num(sim, "center", 50) + (random() * 2 - 1) * num(sim, "amplitude", 1);
    }
    points.push({ t, v: finish(sim, v) });
  }
  return points;
}

function steppedSeries(
  family: SimFamily,
  sim: Simulation,
  start: number,
  end: number,
  random: () => number,
  states: number[]
): PreviewPoint[] {
  const minDwell = num(sim, "min_dwell_s", 30);
  const maxDwell = num(sim, "max_dwell_s", 120);
  const choices = family === "binary" ? [0, 1] : states;
  let held: number | null = null;
  let until = -Infinity;
  const points: PreviewPoint[] = [];
  for (let t = start; t <= end; t += 1) {
    let v: number;
    if (sim.kind === "square") {
      v = position(sim, t) < num(sim, "duty", 0.5) ? 1 : 0;
    } else if (sim.kind === "cycle") {
      v = states[Math.floor((t + num(sim, "phase_s", 0)) / num(sim, "dwell_s", 60)) % states.length];
    } else {
      if (held === null) held = choices[Math.floor(random() * choices.length)];
      else if (t >= until) {
        // Another state, as simulation.py picks: the device never holds one across two dwells.
        const current: number = held;
        const others: number[] = choices.filter((c) => c !== current);
        const pool: number[] = others.length > 0 ? others : choices;
        held = pool[Math.floor(random() * pool.length)];
      }
      if (t >= until) until = t + minDwell + random() * (maxDwell - minDwell);
      v = held;
    }
    if (points.length === 0 || points[points.length - 1].v !== v) points.push({ t, v });
  }
  const last = points[points.length - 1];
  if (last.t !== end) points.push({ t: end, v: last.v });
  return points;
}

export function simulatePreview(family: SimFamily, sim: Simulation, now: number, stateCount = 2): PreviewSeries | null {
  if (sim.kind === "follow") return null;
  const states = statesOf(sim, stateCount);
  if (family === "multi-state" && states.length === 0) return null;
  const start = Math.floor(now);
  const end = start + previewWindow(sim, stateCount);
  const random = seededRandom(1);
  const points =
    family === "analog" ? analogSeries(sim, start, end, random) : steppedSeries(family, sim, start, end, random, states);
  return { step: family !== "analog", random: !PERIODIC_KINDS.includes(sim.kind), start, end, points };
}
