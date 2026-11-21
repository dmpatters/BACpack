import { describe, expect, it } from "vitest";
import { formatUtc, formatUtcBare, formatUtcTime } from "./time";

describe("formatUtcTime", () => {
  it("gives the time of day in UTC", () => {
    expect(formatUtcTime("2026-10-16T10:48:02+00:00")).toBe("10:48:02 UTC");
    expect(formatUtcTime("2026-10-16T05:48:02-05:00")).toBe("10:48:02 UTC");
  });
});

describe("formatUtcBare", () => {
  it("formats without the zone for a column already headed UTC", () => {
    expect(formatUtcBare("2026-09-09T12:00:00Z")).toBe("2026-09-09 12:00:00");
    expect(formatUtcBare("2026-09-30T00:12:53.123456")).toBe("2026-09-30 00:12:53");
  });
});

describe("formatUtc", () => {
  it("formats an ISO UTC timestamp", () => {
    expect(formatUtc("2026-09-30T00:12:53+00:00")).toBe("2026-09-30 00:12:53 UTC");
    expect(formatUtc("2026-09-30T00:12:53Z")).toBe("2026-09-30 00:12:53 UTC");
  });

  it("converts another offset to UTC", () => {
    expect(formatUtc("2026-09-29T19:12:53-05:00")).toBe("2026-09-30 00:12:53 UTC");
  });

  it("treats a timestamp with no zone as UTC and drops microseconds", () => {
    expect(formatUtc("2026-09-29T23:59:48.300000")).toBe("2026-09-29 23:59:48 UTC");
  });

  it("leaves text it can't parse alone", () => {
    expect(formatUtc("soon")).toBe("soon");
    expect(formatUtcTime("soon")).toBe("soon");
  });

  it("still names the zone for a time already in display form", () => {
    expect(formatUtc("2026-09-30 00:12:53")).toBe("2026-09-30 00:12:53 UTC");
    expect(formatUtcTime("2026-09-30 00:12:53")).toBe("00:12:53 UTC");
  });
});
