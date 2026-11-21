import { describe, expect, it } from "vitest";
import { previewWindow, seededRandom, simulatePreview, statesOf } from "./simPreview";

// 1,800,000,000 is a whole number of 60, 120 and 600-second cycles.
const now = 1_800_000_000;
const offsets = (points: { t: number; v: number }[]) => points.map((p) => [p.t - now, p.v]);

describe("previewWindow", () => {
  it("shows about three cycles, from 5 to 20 minutes", () => {
    expect(previewWindow({ kind: "sine", period_s: 600 }, 2)).toBe(1200);
    expect(previewWindow({ kind: "sine", period_s: 200 }, 2)).toBe(600);
    expect(previewWindow({ kind: "square", period_s: 60 }, 2)).toBe(300);
    expect(previewWindow({ kind: "cycle", dwell_s: 60 }, 4)).toBe(720);
    expect(previewWindow({ kind: "noise", center: 50, amplitude: 1 }, 2)).toBe(1200);
  });
});

describe("simulatePreview: analog", () => {
  const sine = { kind: "sine", center: 50, amplitude: 10, period_s: 600, interval_s: 5, decimals: 2 };

  it("follows the wall clock, one sample per update", () => {
    const series = simulatePreview("analog", sine, now)!;
    expect(series.step).toBe(false);
    expect(series.random).toBe(false);
    expect(series.end - series.start).toBe(1200);
    expect(series.points.slice(0, 2)).toEqual([
      { t: now, v: 50 },
      { t: now + 5, v: 50.52 },
    ]);
  });

  it("shifts the cycle by the phase offset", () => {
    expect(simulatePreview("analog", { ...sine, phase_s: 150 }, now)!.points[0].v).toBe(60);
  });

  it("keeps values inside Never below and Never above", () => {
    const values = simulatePreview("analog", { ...sine, max: 55, min: 42 }, now)!.points.map((p) => p.v);
    expect(Math.max(...values)).toBe(55);
    expect(Math.min(...values)).toBe(42);
  });

  it("draws one repeatable example run for a random walk, inside low and high", () => {
    const walk = { kind: "random_walk", start: 50, max_step: 5, low: 40, high: 60, interval_s: 5, decimals: 2 };
    const first = simulatePreview("analog", walk, now)!;
    expect(first.random).toBe(true);
    expect(simulatePreview("analog", walk, now)).toEqual(first);
    expect(first.points.every((p) => p.v >= 40 && p.v <= 60)).toBe(true);
  });

  it("has nothing to draw for Follow", () => {
    expect(simulatePreview("analog", { kind: "follow", source: "analog-value:3", scale: 1, offset: 0 }, now)).toBeNull();
  });
});

describe("simulatePreview: binary and multi-state", () => {
  it("steps a binary square wave at its duty", () => {
    const series = simulatePreview("binary", { kind: "square", period_s: 120, duty: 0.5 }, now)!;
    expect(series.step).toBe(true);
    expect(offsets(series.points)).toEqual([
      [0, 1],
      [60, 0],
      [120, 1],
      [180, 0],
      [240, 1],
      [300, 0],
      [360, 1],
    ]);
  });

  it("cycles through the states on the clock", () => {
    const series = simulatePreview("multi-state", { kind: "cycle", dwell_s: 60 }, now, 4)!;
    expect(offsets(series.points).slice(0, 5)).toEqual([
      [0, 1],
      [60, 2],
      [120, 3],
      [180, 4],
      [240, 1],
    ]);
    const chosen = simulatePreview("multi-state", { kind: "cycle", dwell_s: 60, states: [2, 4] }, now, 4)!;
    expect(new Set(chosen.points.map((p) => p.v))).toEqual(new Set([2, 4]));
  });

  it("holds each random value at least the shortest dwell", () => {
    const series = simulatePreview("binary", { kind: "random", min_dwell_s: 30, max_dwell_s: 120 }, now)!;
    expect(series.random).toBe(true);
    const changes = series.points.slice(0, -1);
    for (let i = 1; i < changes.length; i++) expect(changes[i].t - changes[i - 1].t).toBeGreaterThanOrEqual(30);
    expect(series.points.every((p) => p.v === 0 || p.v === 1)).toBe(true);
  });

  it("moves a multi-state random to another state at each dwell end, as the device does", () => {
    const series = simulatePreview("multi-state", { kind: "random", min_dwell_s: 30, max_dwell_s: 31 }, now, 3)!;
    const changes = series.points.slice(0, -1);
    expect(changes.length).toBeGreaterThan(10);
    for (let i = 1; i < changes.length; i++) expect(changes[i].t - changes[i - 1].t).toBeLessThanOrEqual(31);
  });

  it("has nothing to draw when no chosen state fits the object", () => {
    expect(statesOf({ kind: "cycle", states: [2, 5] }, 4)).toEqual([2]);
    expect(simulatePreview("multi-state", { kind: "cycle", dwell_s: 60, states: [5, 6] }, now, 4)).toBeNull();
  });
});

describe("seededRandom", () => {
  it("repeats for a seed and stays in [0, 1)", () => {
    const a = seededRandom(1);
    const b = seededRandom(1);
    const draws = Array.from({ length: 50 }, () => a());
    expect(draws).toEqual(Array.from({ length: 50 }, () => b()));
    expect(draws.every((x) => x >= 0 && x < 1)).toBe(true);
  });
});
