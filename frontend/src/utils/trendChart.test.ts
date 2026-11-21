import { describe, expect, it } from "vitest";
import type { TrendRangeDTO } from "../api/types";
import { gapText, linePath, maxPointsFor, nearestIndex, segments, toPoints } from "./trendChart";

const START = 1_800_000_000; // 2027-01-15 08:00 UTC
const END = START + 3600;
const iso = (t: number) => new Date(t * 1000).toISOString();

const EMPTY: TrendRangeDTO = {
  mode: "interval",
  interval_seconds: 5,
  step: false,
  samples: [],
  carry_in: null,
  gaps: [],
  thinned: false,
  count_in_range: 0,
  earliest_sample: null,
  start: iso(START),
  end: iso(END),
};

describe("toPoints", () => {
  it("pins the carry-in at the left edge of a step trend", () => {
    const range = {
      ...EMPTY,
      step: true,
      carry_in: { timestamp: "2027-01-15T07:59:00+00:00", value: 1 },
      samples: [{ timestamp: "2027-01-15T08:00:30+00:00", value: 2 }],
    };
    expect(toPoints(range, START, END)).toEqual([
      { t: START, v: 1 },
      { t: START + 30, v: 2 },
    ]);
  });

  it("drops samples outside the range", () => {
    const range = {
      ...EMPTY,
      mode: "cov" as const,
      samples: [
        { timestamp: "2027-01-15T08:00:30+00:00", value: 2 },
        { timestamp: "2027-01-15T09:00:30+00:00", value: 3 },
      ],
    };
    expect(toPoints(range, START, END)).toEqual([
      { t: START + 30, v: 2 },
      { t: END, v: 2 },
    ]);
  });

  it("holds a COV trend's last value to the end", () => {
    const range = { ...EMPTY, mode: "cov" as const, samples: [{ timestamp: "2027-01-15T08:00:30+00:00", value: 2 }] };
    expect(toPoints(range, START, END).at(-1)).toEqual({ t: END, v: 2 });
  });

  it("leaves an interval analog line to start at its first sample", () => {
    const range = {
      ...EMPTY,
      carry_in: { timestamp: "2027-01-15T07:59:00+00:00", value: 1 },
      samples: [{ timestamp: "2027-01-15T08:00:30+00:00", value: 2 }],
    };
    expect(toPoints(range, START, END)).toEqual([{ t: START + 30, v: 2 }]);
  });
});

describe("segments", () => {
  it("breaks the line at a gap and never joins across it", () => {
    const points = [0, 5, 10, 60, 65].map((t) => ({ t: START + t, v: 1 }));
    const gaps = [{ start: iso(START + 10), end: iso(START + 60) }];
    expect(segments(points, gaps).map((s) => s.length)).toEqual([3, 2]);
  });

  it("keeps one segment without gaps", () => {
    const points = [0, 5].map((t) => ({ t: START + t, v: 1 }));
    expect(segments(points, [])).toEqual([points]);
  });
});

describe("linePath", () => {
  const x = (t: number) => t - START;
  const y = (v: number) => 100 - v;
  it("draws a line or a step", () => {
    const seg = [
      { t: START, v: 10 },
      { t: START + 10, v: 20 },
    ];
    expect(linePath(seg, x, y, false)).toBe("M0.0,90.0 L10.0,80.0");
    expect(linePath(seg, x, y, true)).toBe("M0.0,90.0 L10.0,90.0 L10.0,80.0");
  });
});

describe("helpers", () => {
  it("picks about one point per 2 px", () => {
    expect(maxPointsFor(1200)).toBe(600);
    expect(maxPointsFor(4)).toBe(10);
    expect(maxPointsFor(20000)).toBe(5000);
  });

  it("finds the nearest sample", () => {
    const points = [0, 10, 20].map((t) => ({ t, v: 0 }));
    expect(nearestIndex(points, 14)).toBe(1);
    expect(nearestIndex(points, 16)).toBe(2);
    expect(nearestIndex(points, -5)).toBe(0);
  });

  it("names gaps in UTC, with dates when one spans midnight", () => {
    expect(gapText([{ start: "2027-01-15T00:00:00+00:00", end: "2027-01-15T01:00:00+00:00" }])).toBe("no samples 00:00–01:00");
    expect(gapText([{ start: "2027-01-15T23:00:00+00:00", end: "2027-01-16T01:00:00+00:00" }])).toBe(
      "no samples 01-15 23:00–01-16 01:00"
    );
    const four = [0, 1, 2, 3].map((h) => ({ start: iso(START + h * 7200), end: iso(START + h * 7200 + 600) }));
    expect(gapText(four)).toBe("no samples 08:00–08:10, 10:00–10:10, 12:00–12:10 and 1 more");
    expect(gapText([])).toBe("");
  });

  it("gives a gap shorter than 10 minutes its seconds, so it doesn't read 21:13–21:13", () => {
    expect(gapText([{ start: "2026-10-16T21:13:14+00:00", end: "2026-10-16T21:13:41+00:00" }])).toBe(
      "no samples 21:13:14–21:13:41"
    );
  });
});
