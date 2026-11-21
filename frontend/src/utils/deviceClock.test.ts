import { describe, expect, it } from "vitest";
import { describeOffset, toDatetimeLocal, wallTime } from "./deviceClock";

describe("describeOffset", () => {
  it("calls sub-second offsets in sync", () => {
    expect(describeOffset(0)).toBe("in sync with host");
    expect(describeOffset(0.4)).toBe("in sync with host");
    expect(describeOffset(-0.9)).toBe("in sync with host");
  });
  it("describes ahead and behind", () => {
    expect(describeOffset(192)).toBe("+3m 12s ahead of host");
    expect(describeOffset(-3600)).toBe("-1h 0m 0s behind host");
    expect(describeOffset(5)).toBe("+5s ahead of host");
    expect(describeOffset(2 * 86400 + 61)).toBe("+2d 0h 1m 1s ahead of host");
  });
});

describe("device time strings", () => {
  it("reads the wall time without parsing the zone", () => {
    expect(wallTime("2026-09-25T14:03:12-04:00")).toBe("2026-09-25 14:03:12");
    expect(toDatetimeLocal("2026-09-25T14:03:12+05:30")).toBe("2026-09-25T14:03:12");
  });
});
