import { describe, expect, it } from "vitest";
import { isUtcMidnight, timeTicks, utcClock, utcDate, valueTicks } from "./chartAxes";

describe("valueTicks", () => {
  it("covers the values in even steps", () => {
    expect(valueTicks(40, 60)).toEqual([40, 50, 60]);
    expect(valueTicks(49.5, 50.5)).toEqual([49.5, 50, 50.5]);
    expect(valueTicks(42, 55)).toEqual([40, 45, 50, 55]);
  });

  it("gives a flat line some room", () => {
    expect(valueTicks(50, 50)).toEqual([49, 50, 51]);
  });
});

describe("timeTicks", () => {
  const start = 1_800_000_000; // 08:00:00 UTC
  it("picks an even minute step for the window", () => {
    expect(timeTicks(start, start + 1200).map(utcClock)).toEqual(["08:00", "08:05", "08:10", "08:15", "08:20"]);
    expect(timeTicks(start, start + 360).map(utcClock)).toEqual(["08:00", "08:01", "08:02", "08:03", "08:04", "08:05", "08:06"]);
    expect(timeTicks(start + 30, start + 750).map(utcClock)).toEqual(["08:02", "08:04", "08:06", "08:08", "08:10", "08:12"]);
  });
});

describe("timeTicks over long ranges", () => {
  const start = 1_800_000_000; // 2027-01-15 08:00 UTC
  it("steps by hours and days", () => {
    expect(timeTicks(start, start + 6 * 3600).map(utcClock)).toEqual(["08:00", "09:00", "10:00", "11:00", "12:00", "13:00", "14:00"]);
    expect(timeTicks(start, start + 24 * 3600).length).toBeLessThanOrEqual(7);
    expect(timeTicks(start, start + 30 * 86400).length).toBeLessThanOrEqual(7);
    expect(timeTicks(start, start + 30 * 86400).length).toBeGreaterThanOrEqual(3);
  });

  it("names the date at midnight", () => {
    const midnight = 1_800_057_600; // 2027-01-16 00:00 UTC
    expect(isUtcMidnight(midnight)).toBe(true);
    expect(isUtcMidnight(start)).toBe(false);
    expect(utcDate(midnight)).toBe("01-16");
  });
});
