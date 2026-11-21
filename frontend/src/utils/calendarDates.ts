import type { CalendarEntry, CalendarEntryKind } from "../api/types";

// Calendar entries as the device evaluates them: bacpypes3's match_date,
// match_date_range and match_weeknday (local/schedule.py), for the wildcards
// BACpack stores ("*", and "last" for week of month; calendar.py).

export interface DateParts {
  year: number;
  month: number;
  day: number;
}

export const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];
export const WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
export const WEEKDAY_SHORT = WEEKDAY_NAMES.map((name) => name.slice(0, 3));
export const WEEK_NAMES = ["First", "Second", "Third", "Fourth", "Fifth"];

export const ENTRY_KIND_LABELS: Record<CalendarEntryKind, string> = {
  date: "Date",
  "date-range": "Range",
  "week-and-day": "Pattern",
  "calendar-reference": "Calendar",
};

// device_time is the device's wall time ("2026-10-12T08:26:00-04:00"); its
// first ten characters are the device's date, whatever the browser's zone.
export function deviceDate(deviceTime: string): DateParts {
  const [year, month, day] = deviceTime.slice(0, 10).split("-").map(Number);
  return { year, month, day };
}

const pad = (n: number) => String(n).padStart(2, "0");

export function isoDate(d: DateParts): string {
  return `${d.year}-${pad(d.month)}-${pad(d.day)}`;
}

// Day arithmetic in UTC, so no daylight-saving day is 23 or 25 hours.
const toUtc = (d: DateParts) => Date.UTC(d.year, d.month - 1, d.day);

export function addDays(d: DateParts, n: number): DateParts {
  const x = new Date(toUtc(d) + n * 86_400_000);
  return { year: x.getUTCFullYear(), month: x.getUTCMonth() + 1, day: x.getUTCDate() };
}

export function weekday(d: DateParts): number {
  return ((new Date(toUtc(d)).getUTCDay() + 6) % 7) + 1;
}

const daysInMonth = (year: number, month: number) => new Date(Date.UTC(year, month, 0)).getUTCDate();

export function component(value: string | number | null | undefined): number | "*" | "last" {
  if (value === null || value === undefined || value === "" || value === "*") return "*";
  if (value === "last") return "last";
  return Number(value);
}

function parseIso(value: string | null | undefined): DateParts | null {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const [year, month, day] = value.split("-").map(Number);
  return { year, month, day };
}

export function entryMatches(entry: CalendarEntry, d: DateParts): boolean {
  switch (entry.kind) {
    case "date": {
      const [year, month, day, dow] = [entry.year, entry.month, entry.day, entry.day_of_week].map(component);
      return (
        (year === "*" || year === d.year) &&
        (month === "*" || month === d.month) &&
        (day === "*" || day === d.day) &&
        (dow === "*" || dow === weekday(d))
      );
    }
    case "date-range": {
      const start = parseIso(entry.start);
      const end = parseIso(entry.end);
      return start !== null && end !== null && toUtc(d) >= toUtc(start) && toUtc(d) <= toUtc(end);
    }
    case "week-and-day": {
      const [month, week, dow] = [entry.month, entry.week_of_month, entry.day_of_week].map(component);
      if (month !== "*" && month !== d.month) return false;
      if (dow !== "*" && dow !== weekday(d)) return false;
      if (week === "*") return true;
      if (week === "last") return d.day > daysInMonth(d.year, d.month) - 7;
      return d.day >= week * 7 - 6 && d.day <= week * 7;
    }
    default:
      return false;
  }
}

// Eight years always reach the next February 29.
const HORIZON_DAYS = 366 * 8;

export function nextMatches(entry: CalendarEntry, from: DateParts, count = 3): string[] {
  const found: string[] = [];
  for (let i = 0; i < HORIZON_DAYS && found.length < count; i++) {
    const d = addDays(from, i);
    if (entryMatches(entry, d)) found.push(isoDate(d));
  }
  return found;
}

const capitalize = (text: string) => text[0].toUpperCase() + text.slice(1);

function describeDate(entry: CalendarEntry): string {
  const [year, month, day, dow] = [entry.year, entry.month, entry.day, entry.day_of_week].map(component);
  const monthName = typeof month === "number" ? MONTH_NAMES[month - 1] : null;
  const weekdayName = typeof dow === "number" ? WEEKDAY_NAMES[dow - 1] : null;
  let text: string;
  if (weekdayName && day === "*") text = monthName ? `every ${weekdayName} in ${monthName}` : `every ${weekdayName}`;
  else if (monthName && day !== "*") text = `${monthName} ${day}`;
  else if (monthName) text = `every day in ${monthName}`;
  else if (day !== "*") text = `day ${day} of every month`;
  else text = "every day";
  if (year !== "*") text += `, ${year}`;
  else if (monthName && day !== "*") text += ", every year";
  if (weekdayName && day !== "*") text += `, when it is a ${weekdayName}`;
  return capitalize(text);
}

function describePattern(entry: CalendarEntry): string {
  const [month, week, dow] = [entry.month, entry.week_of_month, entry.day_of_week].map(component);
  const monthName = typeof month === "number" ? MONTH_NAMES[month - 1] : null;
  const weekdayName = typeof dow === "number" ? WEEKDAY_NAMES[dow - 1] : null;
  const of = monthName ?? "every month";
  if (week === "*") {
    if (weekdayName) return monthName ? `Every ${weekdayName} in ${monthName}` : `Every ${weekdayName}`;
    return monthName ? `Every day in ${monthName}` : "Every day";
  }
  const which = week === "last" ? "Last" : WEEK_NAMES[week - 1];
  if (weekdayName) return `${which} ${weekdayName} of ${of}`;
  return week === "last" ? `The last 7 days of ${of}` : `Days ${week * 7 - 6}–${Math.min(week * 7, 31)} of ${of}`;
}

export function describeEntry(entry: CalendarEntry, calendarName?: (instance: number) => string | undefined): string {
  switch (entry.kind) {
    case "date":
      return describeDate(entry);
    case "date-range":
      return entry.start && entry.end ? `${entry.start} to ${entry.end}` : "A date range without both ends yet";
    case "week-and-day":
      return describePattern(entry);
    case "calendar-reference": {
      const n = entry.instance_number;
      if (n == null) return "No calendar chosen yet";
      return `Whenever calendar ${calendarName?.(n) ?? n} is in effect`;
    }
    default:
      return "";
  }
}

export function rawEntry(entry: CalendarEntry): string {
  const c = (value: string | number | null | undefined) => String(component(value));
  switch (entry.kind) {
    case "date":
      return `year ${c(entry.year)} · month ${c(entry.month)} · day ${c(entry.day)} · weekday ${c(entry.day_of_week)}`;
    case "date-range":
      return `${entry.start || "?"} – ${entry.end || "?"}`;
    case "week-and-day":
      return `month ${c(entry.month)} · week ${c(entry.week_of_month)} · weekday ${c(entry.day_of_week)}`;
    case "calendar-reference":
      return `calendar ${entry.instance_number ?? "?"}`;
    default:
      return "";
  }
}

// What stops an entry being added. One that matches no date is allowed, with
// a warning (nextMatches says so).
export function entryProblem(entry: CalendarEntry): string | null {
  if (entry.kind === "date-range") {
    const start = parseIso(entry.start);
    const end = parseIso(entry.end);
    if (!start || !end) return "Choose a start and an end date.";
    if (toUtc(end) < toUtc(start)) return "End is before start.";
  }
  if (entry.kind === "calendar-reference" && entry.instance_number == null) return "Choose a calendar.";
  return null;
}
