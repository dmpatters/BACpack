import { describe, expect, it } from "vitest";
import {
  BLANK_DEVICE,
  describeWriteStatus,
  emptyWeeklySchedule,
  newRemoteScheduleWriteTarget,
  writeTargetStatusKey,
  newCalendarEntry,
  newScheduleException,
  newScheduleWriteTarget,
  normalizeExceptionSchedule,
  normalizeTimeValues,
  normalizeWeeklySchedule,
} from "./scheduleForm";

describe("emptyWeeklySchedule", () => {
  it("returns exactly 7 empty days", () => {
    const week = emptyWeeklySchedule();
    expect(week).toHaveLength(7);
    expect(week.every((day) => day.length === 0)).toBe(true);
  });

  it("returns independent array instances per day", () => {
    const week = emptyWeeklySchedule();
    week[0].push({ time: "08:00", value: "" });
    expect(week[1]).toHaveLength(0);
  });
});

describe("newCalendarEntry", () => {
  it("defaults a 'date' entry to all wildcards", () => {
    expect(newCalendarEntry("date")).toEqual({ kind: "date", year: "*", month: "*", day: "*" });
  });

  it("defaults a 'date-range' entry with blank bounds", () => {
    expect(newCalendarEntry("date-range")).toEqual({ kind: "date-range", start: "", end: "" });
  });

  it("defaults a 'week-and-day' entry to all wildcards", () => {
    expect(newCalendarEntry("week-and-day")).toEqual({
      kind: "week-and-day",
      month: "*",
      week_of_month: "*",
      day_of_week: "*",
    });
  });

  it("defaults a 'calendar-reference' entry with no instance chosen yet", () => {
    expect(newCalendarEntry("calendar-reference")).toEqual({
      kind: "calendar-reference",
      instance_number: null,
    });
  });
});

describe("newScheduleException", () => {
  it("starts with a 'date' period, priority 1, and no time-values", () => {
    const event = newScheduleException();
    expect(event.period.kind).toBe("date");
    expect(event.priority).toBe(1);
    expect(event.time_values).toEqual([]);
  });
});

describe("newScheduleWriteTarget", () => {
  it("defaults property_identifier to presentValue", () => {
    expect(newScheduleWriteTarget("analog-value", 3)).toEqual({
      object_type: "analog-value",
      instance_number: 3,
      property_identifier: "presentValue",
    });
  });
});

describe("normalizeTimeValues", () => {
  it("converts blank value text to null (relinquish to schedule_default)", () => {
    expect(normalizeTimeValues([{ time: "08:00", value: "  " }])).toEqual([{ time: "08:00", value: null }]);
  });

  it("trims a real value", () => {
    expect(normalizeTimeValues([{ time: "08:00", value: " 72.5 " }])).toEqual([
      { time: "08:00", value: "72.5" },
    ]);
  });

  it("leaves an already-null value alone", () => {
    expect(normalizeTimeValues([{ time: "08:00", value: null }])).toEqual([{ time: "08:00", value: null }]);
  });
});

describe("normalizeWeeklySchedule / normalizeExceptionSchedule", () => {
  it("normalizes every day's time-values", () => {
    const week = emptyWeeklySchedule();
    week[0] = [{ time: "08:00", value: " 1.0 " }, { time: "17:00", value: "" }];
    const normalized = normalizeWeeklySchedule(week);
    expect(normalized[0]).toEqual([
      { time: "08:00", value: "1.0" },
      { time: "17:00", value: null },
    ]);
  });

  it("normalizes every exception event's time-values, leaving the period alone", () => {
    const events = [
      {
        period: newCalendarEntry("date"),
        priority: 1,
        time_values: [{ time: "08:00", value: " 99.0 " }],
      },
    ];
    const normalized = normalizeExceptionSchedule(events);
    expect(normalized[0].time_values).toEqual([{ time: "08:00", value: "99.0" }]);
    expect(normalized[0].period).toEqual(newCalendarEntry("date"));
  });
});

describe("remote schedule write targets", () => {
  it("builds a remote target defaulting to present-value", () => {
    expect(newRemoteScheduleWriteTarget()).toEqual({
      device_instance: BLANK_DEVICE,
      object_type: "analog-value",
      instance_number: 0,
      property_identifier: "present-value",
    });
    expect(newRemoteScheduleWriteTarget(599005).device_instance).toBe(599005);
  });

  it("shows a binary target's value as active/inactive", () => {
    const entry = { last_sent_at: "2026-09-28T12:00:00+00:00", last_sent_value: 1, error: null, suppressed_by_dcc: false };
    expect(describeWriteStatus(entry, "binary-output")).toBe("sent active at 2026-09-28 12:00:00 UTC");
    expect(describeWriteStatus({ ...entry, last_sent_value: 0 }, "binary-value")).toBe("sent inactive at 2026-09-28 12:00:00 UTC");
    expect(describeWriteStatus(entry, "multi-state-value")).toBe("sent 1 at 2026-09-28 12:00:00 UTC");
  });

  it("keys a target the way the server's status does", () => {
    expect(
      writeTargetStatusKey({
        device_instance: 599005,
        object_type: "analog-value",
        instance_number: 3,
        property_identifier: "present-value",
      })
    ).toBe("599005:analog-value:3:present-value");
  });

  it("describes each status", () => {
    expect(describeWriteStatus(undefined)).toBe("not sent yet");
    expect(
      describeWriteStatus({ last_sent_at: null, last_sent_value: null, error: null, suppressed_by_dcc: true })
    ).toBe("suppressed by DCC");
    expect(
      describeWriteStatus({ last_sent_at: null, last_sent_value: null, error: "timeout", suppressed_by_dcc: false })
    ).toBe("error: timeout");
    expect(
      describeWriteStatus({
        last_sent_at: "2026-09-28T12:00:00+00:00",
        last_sent_value: 21.5,
        error: null,
        suppressed_by_dcc: false,
      })
    ).toBe("sent 21.5 at 2026-09-28 12:00:00 UTC");
    expect(
      describeWriteStatus({
        last_sent_at: "2026-09-28T12:00:00+00:00",
        last_sent_value: null,
        error: null,
        suppressed_by_dcc: false,
      })
    ).toBe("relinquished at 2026-09-28 12:00:00 UTC");
  });

  it("keeps the last delivery next to a later error or DCC", () => {
    const lastSent = { last_sent_at: "2026-09-28T12:00:00+00:00", last_sent_value: 55 };
    expect(describeWriteStatus({ ...lastSent, error: "timeout", suppressed_by_dcc: false })).toBe(
      "error: timeout (last sent 55 at 2026-09-28 12:00:00 UTC)"
    );
    expect(describeWriteStatus({ ...lastSent, error: null, suppressed_by_dcc: true })).toBe(
      "suppressed by DCC (last sent 55 at 2026-09-28 12:00:00 UTC)"
    );
  });
});
