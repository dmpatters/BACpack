import type {
  CalendarEntry,
  CalendarEntryKind,
  ScheduleDaySchedule,
  ScheduleException,
  ScheduleTimeValue,
  ScheduleWriteStatusEntry,
  ScheduleWriteTarget,
} from "../api/types";
import { formatUtc } from "./time";

// Pure builders and defaults for the Schedule and Dates tabs, kept
// framework-free like objectForm.ts.

// A fresh weekly_schedule with no configured time-values on any day -- a
// structurally valid "always uses schedule_default" starting point (see
// validate_schedule_config: an empty list per day isn't rejected, only a
// wrong *count* of days or a missing weekly_schedule/exception_schedule
// altogether is).
export function emptyWeeklySchedule(): ScheduleDaySchedule[] {
  return Array.from({ length: 7 }, () => []);
}

export function newCalendarEntry(kind: CalendarEntryKind = "date"): CalendarEntry {
  switch (kind) {
    case "date":
      return { kind, year: "*", month: "*", day: "*" };
    case "date-range":
      return { kind, start: "", end: "" };
    case "week-and-day":
      return { kind, month: "*", week_of_month: "*", day_of_week: "*" };
    case "calendar-reference":
      return { kind, instance_number: null };
  }
}

export function newScheduleException(): ScheduleException {
  return { period: newCalendarEntry("date"), priority: 1, time_values: [] };
}

export function newScheduleWriteTarget(objectType: string, instanceNumber: number): ScheduleWriteTarget {
  return { object_type: objectType, instance_number: instanceNumber, property_identifier: "presentValue" };
}

// A remote row whose device instance hasn't been entered yet. Not
// null: a null device_instance means "this device" (isRemoteWriteTarget).
export const BLANK_DEVICE = -1;

export function newRemoteScheduleWriteTarget(deviceInstance: number = BLANK_DEVICE): ScheduleWriteTarget {
  return { device_instance: deviceInstance, object_type: "analog-value", instance_number: 0, property_identifier: "present-value" };
}

export function isRemoteWriteTarget(target: ScheduleWriteTarget): boolean {
  return target.device_instance !== undefined && target.device_instance !== null;
}

// Same key the device process writes schedule_write_status under.
export function writeTargetStatusKey(target: ScheduleWriteTarget): string {
  return `${target.device_instance}:${target.object_type}:${target.instance_number}:${target.property_identifier}`;
}

function describeLastSent(entry: ScheduleWriteStatusEntry, objectType?: string): string | null {
  if (!entry.last_sent_at) return null;
  const at = formatUtc(entry.last_sent_at);
  if (entry.last_sent_value === null) return `relinquished at ${at}`;
  const value = entry.last_sent_value;
  // A binary target is sent its enumeration number; show the name.
  if (objectType?.startsWith("binary-") && (value === 0 || value === 1)) {
    return `sent ${value === 1 ? "active" : "inactive"} at ${at}`;
  }
  return `sent ${JSON.stringify(value)} at ${at}`;
}

export function describeWriteStatus(entry: ScheduleWriteStatusEntry | undefined, objectType?: string): string {
  if (!entry) return "not sent yet";
  const lastSent = describeLastSent(entry, objectType);
  // The device keeps the last delivery across a restart, so an error can
  // still say what the remote was last given.
  const since = lastSent ? ` (last ${lastSent})` : "";
  if (entry.suppressed_by_dcc) return `suppressed by DCC${since}`;
  if (entry.error) return `error: ${entry.error}${since}`;
  return lastSent ?? "not sent yet";
}

// Blank text in a time-value's value field means "relinquish to
// schedule_default at this time" (BACnet Null choice) -- mirrors
// TimeValue's own null-means-relinquish convention in bacnet.py.
export function normalizeTimeValues(entries: ScheduleTimeValue[]): ScheduleTimeValue[] {
  return entries.map((e) => ({ time: e.time, value: e.value && e.value.trim() ? e.value.trim() : null }));
}

export function normalizeWeeklySchedule(days: ScheduleDaySchedule[]): ScheduleDaySchedule[] {
  return days.map(normalizeTimeValues);
}

export function normalizeExceptionSchedule(events: ScheduleException[]): ScheduleException[] {
  return events.map((e) => ({ ...e, time_values: normalizeTimeValues(e.time_values) }));
}
