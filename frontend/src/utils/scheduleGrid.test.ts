import { describe, expect, it } from "vitest";
import type { ScheduleTimeValue } from "../api/types";
import {
  copyDay,
  daySegments,
  daySummary,
  duplicateTimes,
  entryLabel,
  firstOtherValue,
  minutes,
  nextEntryTime,
  nowMarker,
  timeProblem,
  valueName,
  weekTones,
  type ValueNaming,
} from "./scheduleGrid";

const binary: ValueNaming = { family: "binary", states: [], unit: "", defaultValue: "inactive" };
const multi: ValueNaming = { family: "multi-state", states: ["Occupied", "Standby", "Unoccupied", ""], unit: "", defaultValue: "3" };
const analog: ValueNaming = { family: "analog", states: [], unit: "°F", defaultValue: "70" };
const tv = (time: string, value: string | null): ScheduleTimeValue => ({ time, value });
const week = (monday: ScheduleTimeValue[]) => [monday, [], [], [], [], [], []];

describe("names", () => {
  it("names values by family", () => {
    expect(valueName("active", binary)).toBe("Active");
    expect(valueName("1", multi)).toBe("Occupied");
    expect(valueName("4", multi)).toBe("State 4");
    expect(valueName("72.5", analog)).toBe("72.5 °F");
  });

  it("reads a NULL or blank entry as the default", () => {
    expect(entryLabel(tv("17:00", null), binary)).toBe("Inactive (default)");
    expect(entryLabel(tv("17:00", ""), multi)).toBe("Unoccupied (default)");
  });

  it("reads times as minutes", () => {
    expect(minutes("08:30")).toBe(510);
    expect(Number.isNaN(minutes(""))).toBe(true);
  });
});

describe("daySegments", () => {
  const tones = (entries: ScheduleTimeValue[], naming: ValueNaming) => weekTones(week(entries), naming);

  it("runs a value until the next entry", () => {
    const entries = [tv("08:00", "active"), tv("17:00", "inactive")];
    expect(daySegments(entries, binary, tones(entries, binary))).toEqual([
      { start: 480 / 1440, span: 540 / 1440, label: "Active", tone: "" },
    ]);
  });

  it("draws nothing for a NULL entry or one equal to the default, and runs the last to midnight", () => {
    const entries = [tv("18:00", "active"), tv("08:00", "active"), tv("12:00", null)];
    expect(daySegments(entries, binary, tones(entries, binary))).toEqual([
      { start: 480 / 1440, span: 240 / 1440, label: "Active", tone: "" },
      { start: 1080 / 1440, span: 360 / 1440, label: "Active", tone: "" },
    ]);
    const equal = [tv("08:00", "70.0")];
    expect(daySegments(equal, analog, tones(equal, analog))).toEqual([]);
  });

  it("joins neighbouring entries of one value into one block", () => {
    const entries = [tv("06:00", "active"), tv("12:00", "active")];
    expect(daySegments(entries, binary, tones(entries, binary))).toEqual([
      { start: 0.25, span: 0.75, label: "Active", tone: "" },
    ]);
  });

  it("tones values by first use across the week, reusing the last tone after three", () => {
    const tones = weekTones([[tv("08:00", "2"), tv("12:00", "1")], [tv("09:00", "4")], [], [], [], [], []], {
      ...multi,
      states: ["A", "B", "C", "D", "E"],
    });
    expect([...tones]).toEqual([
      ["2", ""],
      ["1", "b"],
      ["4", "c"],
    ]);
    const more = weekTones([[tv("08:00", "1"), tv("09:00", "2"), tv("10:00", "4"), tv("11:00", "5")], [], [], [], [], [], []], {
      ...multi,
      states: ["A", "B", "C", "D", "E"],
    });
    expect(more.get("5")).toBe("c");
  });
});

describe("daySummary", () => {
  it("lists the entries in time order with their names", () => {
    expect(daySummary([tv("17:00", null), tv("08:00", "active")], binary)).toBe("08:00 Active · 17:00 Inactive (default)");
  });

  it("says when the default runs all day", () => {
    expect(daySummary([], multi)).toBe("Unoccupied all day (default)");
  });
});

describe("editing", () => {
  it("finds a time a day already has", () => {
    expect([...duplicateTimes([tv("08:00", "1"), tv("09:00", "2"), tv("08:00", "3")])]).toEqual([2]);
    expect(timeProblem([tv("08:00", "1"), tv("08:00", "2")], "Monday")).toBe("Two Monday entries are at 08:00.");
    expect(timeProblem([tv("08:00", "1")], "Monday")).toBeNull();
    expect(timeProblem([tv("08:00", "1"), tv("", "2")], "Monday")).toBe("Monday entry 2 has no time.");
    expect(timeProblem([tv("", "2")], "exception 1")).toBe("Exception 1 entry 1 has no time.");
  });

  it("copies a day as new entries, replacing the chosen days", () => {
    const days = week([tv("08:00", "active")]);
    days[1] = [tv("10:00", "inactive")];
    const copied = copyDay(days, 0, [1, 2]);
    expect(copied[1]).toEqual([tv("08:00", "active")]);
    expect(copied[2]).toEqual([tv("08:00", "active")]);
    expect(copied[1][0]).not.toBe(days[0][0]);
    expect(copied[3]).toBe(days[3]);
  });

  it("starts a new entry an hour after the last", () => {
    expect(nextEntryTime([])).toBe("08:00");
    expect(nextEntryTime([tv("17:00", "1"), tv("08:00", "1")])).toBe("18:00");
    expect(nextEntryTime([tv("23:30", "1")])).toBe("23:59");
  });

  it("starts a new entry on a value other than the default", () => {
    expect(firstOtherValue(binary)).toBe("active");
    expect(firstOtherValue({ ...multi, defaultValue: "1" })).toBe("2");
    expect(firstOtherValue(analog)).toBe("");
  });
});

describe("nowMarker", () => {
  it("places the device's time on its weekday's row", () => {
    // 2026-10-14 is a Wednesday.
    expect(nowMarker("2026-10-14T08:26:00-04:00")).toEqual({ day: 2, at: 506 / 1440, label: "08:26" });
  });
});
