import { describe, expect, it } from "vitest";
import type { CalendarEntry } from "../api/types";
import { deviceDate, describeEntry, entryMatches, entryProblem, nextMatches, rawEntry, weekday } from "./calendarDates";

// 2026-10-12 is a Monday; 2026-10-30, 11-27 and 12-25 are Fridays.
const today = { year: 2026, month: 10, day: 12 };
const date = (year: string, month: string, day: string, day_of_week = "*"): CalendarEntry => ({ kind: "date", year, month, day, day_of_week });
const pattern = (month: string, week_of_month: string, day_of_week: string): CalendarEntry => ({ kind: "week-and-day", month, week_of_month, day_of_week });

describe("dates", () => {
  it("reads the device's own date from its wall time", () => {
    expect(deviceDate("2026-10-12T23:30:00-04:00")).toEqual(today);
  });

  it("numbers weekdays from Monday", () => {
    expect(weekday(today)).toBe(1);
    expect(weekday({ year: 2026, month: 10, day: 18 })).toBe(7);
  });
});

describe("entryMatches", () => {
  it("matches a date with wildcards, a null component counting as any", () => {
    expect(entryMatches(date("*", "12", "25"), { year: 2031, month: 12, day: 25 })).toBe(true);
    expect(entryMatches({ kind: "date", year: null, month: 12, day: 25 }, { year: 2031, month: 12, day: 25 })).toBe(true);
    expect(entryMatches(date("2026", "12", "25"), { year: 2027, month: 12, day: 25 })).toBe(false);
    expect(entryMatches(date("*", "*", "*", "5"), { year: 2026, month: 10, day: 30 })).toBe(true);
    expect(entryMatches(date("*", "*", "*", "5"), today)).toBe(false);
  });

  it("matches a range including both ends", () => {
    const range: CalendarEntry = { kind: "date-range", start: "2026-07-01", end: "2026-07-07" };
    expect(entryMatches(range, { year: 2026, month: 7, day: 1 })).toBe(true);
    expect(entryMatches(range, { year: 2026, month: 7, day: 7 })).toBe(true);
    expect(entryMatches(range, { year: 2026, month: 7, day: 8 })).toBe(false);
  });

  it("matches week of month by day number, and last as the final seven days", () => {
    // November 2026: the 30th is the fifth Monday.
    expect(entryMatches(pattern("*", "5", "1"), { year: 2026, month: 11, day: 30 })).toBe(true);
    expect(entryMatches(pattern("*", "5", "1"), { year: 2026, month: 11, day: 23 })).toBe(false);
    expect(entryMatches(pattern("*", "last", "5"), { year: 2026, month: 10, day: 30 })).toBe(true);
    expect(entryMatches(pattern("*", "last", "5"), { year: 2026, month: 10, day: 23 })).toBe(false);
    expect(entryMatches(pattern("2", "last", "*"), { year: 2028, month: 2, day: 23 })).toBe(true);
    expect(entryMatches(pattern("2", "last", "*"), { year: 2028, month: 2, day: 22 })).toBe(false);
  });
});

describe("nextMatches", () => {
  it("lists the next three dates", () => {
    expect(nextMatches(pattern("*", "last", "5"), today)).toEqual(["2026-10-30", "2026-11-27", "2026-12-25"]);
    expect(nextMatches(date("*", "12", "25"), today)).toEqual(["2026-12-25", "2027-12-25", "2028-12-25"]);
  });

  it("includes today", () => {
    expect(nextMatches(date("2026", "10", "12"), today)).toEqual(["2026-10-12"]);
  });

  it("finds a leap day, and nothing for a date that never comes", () => {
    expect(nextMatches(date("*", "2", "29"), today)).toEqual(["2028-02-29", "2032-02-29"]);
    expect(nextMatches(date("*", "2", "30"), today)).toEqual([]);
    expect(nextMatches(date("2025", "12", "25"), today)).toEqual([]);
  });
});

describe("describeEntry", () => {
  it.each([
    [date("*", "12", "25"), "December 25, every year"],
    [date("2026", "12", "25"), "December 25, 2026"],
    [date("*", "*", "*"), "Every day"],
    [date("*", "12", "*"), "Every day in December"],
    [date("*", "*", "1"), "Day 1 of every month"],
    [date("*", "*", "*", "5"), "Every Friday"],
    [date("*", "12", "*", "5"), "Every Friday in December"],
    [date("*", "*", "13", "5"), "Day 13 of every month, when it is a Friday"],
    [pattern("*", "last", "5"), "Last Friday of every month"],
    [pattern("3", "2", "1"), "Second Monday of March"],
    [pattern("*", "*", "1"), "Every Monday"],
    [pattern("3", "*", "*"), "Every day in March"],
    [pattern("*", "1", "*"), "Days 1–7 of every month"],
    [pattern("*", "last", "*"), "The last 7 days of every month"],
    [{ kind: "date-range", start: "2026-07-01", end: "2026-07-07" } as CalendarEntry, "2026-07-01 to 2026-07-07"],
  ])("describes %o as %s", (entry, text) => {
    expect(describeEntry(entry)).toBe(text);
  });

  it("names a referenced calendar", () => {
    const ref: CalendarEntry = { kind: "calendar-reference", instance_number: 3 };
    expect(describeEntry(ref, (n) => (n === 3 ? "HOLIDAYS" : undefined))).toBe("Whenever calendar HOLIDAYS is in effect");
    expect(describeEntry({ kind: "calendar-reference", instance_number: null })).toBe("No calendar chosen yet");
  });
});

describe("rawEntry", () => {
  it("writes the BACnet form", () => {
    expect(rawEntry(date("*", "12", "25"))).toBe("year * · month 12 · day 25 · weekday *");
    expect(rawEntry(pattern("*", "last", "5"))).toBe("month * · week last · weekday 5");
    expect(rawEntry({ kind: "date-range", start: "2026-07-01", end: "2026-07-07" })).toBe("2026-07-01 – 2026-07-07");
    expect(rawEntry({ kind: "calendar-reference", instance_number: 3 })).toBe("calendar 3");
  });
});

describe("entryProblem", () => {
  it("stops a range without both ends or with its end first, and a reference with no calendar", () => {
    expect(entryProblem({ kind: "date-range", start: "2026-07-01", end: "" })).toBe("Choose a start and an end date.");
    expect(entryProblem({ kind: "date-range", start: "2026-07-07", end: "2026-07-01" })).toBe("End is before start.");
    expect(entryProblem({ kind: "calendar-reference", instance_number: null })).toBe("Choose a calendar.");
    expect(entryProblem(date("*", "2", "30"))).toBeNull();
  });
});
