import type { ScheduleDaySchedule, ScheduleTimeValue } from "../api/types";
import { deviceDate, weekday } from "./calendarDates";
import type { ValueFamily } from "./objectTypes";

// docs/design-system/components/ScheduleGrid. A value runs from its entry's
// time to the next entry's (the last to 24:00); NULL means the default from
// that time, as BACnet's Weekly_Schedule does.

export interface ValueNaming {
  family: ValueFamily;
  states: string[];
  unit: string;
  defaultValue: string;
}

export type Tone = "" | "b" | "c";

export interface Segment {
  start: number;
  span: number;
  label: string;
  tone: Tone;
}

const pad = (n: number) => String(n).padStart(2, "0");

export function minutes(time: string): number {
  const match = /^(\d{2}):(\d{2})/.exec(time);
  return match ? Number(match[1]) * 60 + Number(match[2]) : NaN;
}

const isNull = (value: string | null): value is null | "" => value === null || value.trim() === "";

const sameValue = (family: ValueFamily, a: string, b: string) =>
  family === "analog" ? a.trim() !== "" && b.trim() !== "" && Number(a) === Number(b) : a === b;

const valueKey = (family: ValueFamily, value: string) => (family === "analog" ? String(Number(value)) : value);

export function valueName(value: string, naming: ValueNaming): string {
  if (naming.family === "binary") return value === "active" ? "Active" : value === "inactive" ? "Inactive" : value;
  if (naming.family === "multi-state") {
    const name = naming.states[Number(value) - 1];
    return name?.trim() ? name : `State ${value}`;
  }
  return naming.unit ? `${value} ${naming.unit}` : value;
}

export function defaultName(naming: ValueNaming): string {
  return naming.defaultValue.trim() ? valueName(naming.defaultValue, naming) : "The default";
}

export function entryLabel(tv: ScheduleTimeValue, naming: ValueNaming): string {
  return isNull(tv.value) ? `${defaultName(naming)} (default)` : valueName(tv.value, naming);
}

function inTimeOrder(entries: ScheduleTimeValue[]) {
  return entries
    .map((tv, index) => ({ tv, index, at: minutes(tv.time) }))
    .filter((e) => Number.isFinite(e.at))
    .sort((a, b) => a.at - b.at || a.index - b.index);
}

const drawsNothing = (value: string | null, naming: ValueNaming) =>
  isNull(value) || sameValue(naming.family, value, naming.defaultValue);

const TONES: Tone[] = ["", "b", "c"];

export function weekTones(days: ScheduleDaySchedule[], naming: ValueNaming): Map<string, Tone> {
  const tones = new Map<string, Tone>();
  for (const day of days) {
    for (const { tv } of inTimeOrder(day)) {
      if (drawsNothing(tv.value, naming)) continue;
      const key = valueKey(naming.family, tv.value as string);
      if (!tones.has(key)) tones.set(key, TONES[Math.min(tones.size, TONES.length - 1)]);
    }
  }
  return tones;
}

export function daySegments(entries: ScheduleTimeValue[], naming: ValueNaming, tones: Map<string, Tone>): Segment[] {
  const list = inTimeOrder(entries);
  const segments: (Segment & { key: string })[] = [];
  list.forEach(({ tv, at }, k) => {
    const end = k + 1 < list.length ? list[k + 1].at : 1440;
    if (end <= at || drawsNothing(tv.value, naming)) return;
    const key = valueKey(naming.family, tv.value as string);
    const last = segments[segments.length - 1];
    if (last && last.key === key && Math.abs(last.start + last.span - at / 1440) < 1e-9) {
      last.span += (end - at) / 1440;
      return;
    }
    segments.push({
      key,
      start: at / 1440,
      span: (end - at) / 1440,
      label: valueName(tv.value as string, naming),
      tone: tones.get(key) ?? "c",
    });
  });
  return segments.map(({ start, span, label, tone }) => ({ start, span, label, tone }));
}

export function daySummary(entries: ScheduleTimeValue[], naming: ValueNaming): string {
  const list = inTimeOrder(entries);
  if (list.length === 0) return `${defaultName(naming)} all day (default)`;
  return list.map(({ tv }) => `${tv.time} ${entryLabel(tv, naming)}`).join(" · ");
}

// The backend refuses a day with two entries at one time.
export function duplicateTimes(entries: ScheduleTimeValue[]): Set<number> {
  const seen = new Set<string>();
  const dupes = new Set<number>();
  entries.forEach((tv, i) => {
    if (!tv.time) return;
    if (seen.has(tv.time)) dupes.add(i);
    else seen.add(tv.time);
  });
  return dupes;
}

export function timeProblem(entries: ScheduleTimeValue[], where: string): string | null {
  const blank = entries.findIndex((tv) => !tv.time);
  if (blank >= 0) return `${where.charAt(0).toUpperCase()}${where.slice(1)} entry ${blank + 1} has no time.`;
  const first = duplicateTimes(entries).values().next().value;
  return first === undefined ? null : `Two ${where} entries are at ${entries[first].time}.`;
}

export function copyDay(days: ScheduleDaySchedule[], from: number, to: number[]): ScheduleDaySchedule[] {
  return days.map((day, i) => (to.includes(i) ? days[from].map((tv) => ({ ...tv })) : day));
}

export function nextEntryTime(entries: ScheduleTimeValue[]): string {
  const times = entries.map((tv) => minutes(tv.time)).filter(Number.isFinite);
  if (times.length === 0) return "08:00";
  const next = Math.min(Math.max(...times) + 60, 1439);
  return `${pad(Math.floor(next / 60))}:${pad(next % 60)}`;
}

export function firstOtherValue(naming: ValueNaming): string {
  if (naming.family === "binary") return naming.defaultValue === "active" ? "inactive" : "active";
  if (naming.family === "multi-state") {
    const other = naming.states.findIndex((_, i) => String(i + 1) !== naming.defaultValue);
    return other < 0 ? "" : String(other + 1);
  }
  return "";
}

// device_time is the device's wall time; schedules run on it.
export function nowMarker(deviceTime: string): { day: number; at: number; label: string } {
  const label = deviceTime.slice(11, 16);
  return { day: weekday(deviceDate(deviceTime)) - 1, at: minutes(label) / 1440, label };
}
