import { useRef } from "react";
import type { CalendarEntry, CalendarEntryKind } from "../api/types";
import {
  MONTH_NAMES,
  WEEKDAY_NAMES,
  WEEK_NAMES,
  component,
  describeEntry,
  entryProblem,
  nextMatches,
  type DateParts,
} from "../utils/calendarDates";
import { newCalendarEntry } from "../utils/scheduleForm";
import Callout from "./Callout";
import { Field, FormGrid, Segmented } from "./Form";
import IsoEcho from "./IsoEcho";

const KINDS: { value: CalendarEntryKind; label: string }[] = [
  { value: "date", label: "Date" },
  { value: "date-range", label: "Date range" },
  { value: "week-and-day", label: "Pattern" },
];

const DAYS = Array.from({ length: 31 }, (_, i) => i + 1);

// The years a Date entry offers: last year to ten ahead, plus a stored year
// outside that range so it still shows.
function yearOptions(today: DateParts | null, stored: string | number | null | undefined): number[] {
  const base = today?.year ?? new Date().getFullYear();
  const years = Array.from({ length: 12 }, (_, i) => base - 1 + i);
  const current = component(stored);
  if (typeof current === "number" && !years.includes(current)) years.push(current);
  return years.sort((a, b) => a - b);
}

// docs/design-system/components/CalendarEditor: wildcards are words in
// selects; "Last" exists for week of month only, as BACpack stores it.
export default function CalendarEntryFields({
  idPrefix,
  entry,
  onChange,
  today,
  calendars,
}: {
  idPrefix: string;
  entry: CalendarEntry;
  onChange: (entry: CalendarEntry) => void;
  today: DateParts | null;
  calendars?: { instance_number: number; name: string }[];
}) {
  // What each kind held when the user switched away from it. An entry this
  // component didn't hand out (a form reset, another exception's entry after
  // a removal) starts the memory afresh.
  const stash = useRef<Partial<Record<CalendarEntryKind, CalendarEntry>>>({});
  const emitted = useRef(entry);
  if (entry !== emitted.current) {
    stash.current = {};
    emitted.current = entry;
  }
  const emit = (next: CalendarEntry) => {
    emitted.current = next;
    onChange(next);
  };
  const kinds = calendars ? [...KINDS, { value: "calendar-reference" as const, label: "Calendar" }] : KINDS;
  const set = (patch: Partial<CalendarEntry>) => emit({ ...entry, ...patch });
  const switchKind = (kind: CalendarEntryKind) => {
    stash.current[entry.kind] = entry;
    emit(stash.current[kind] ?? newCalendarEntry(kind));
  };
  const selected = (value: string | number | null | undefined) => String(component(value));
  const problem = entryProblem(entry);
  const calendarName = (n: number) => calendars?.find((c) => c.instance_number === n)?.name;
  const searchable = today !== null && problem === null && entry.kind !== "calendar-reference";
  const next = searchable ? nextMatches(entry, today) : [];

  const monthSelect = (
    <Field label="Month">
      <select id={`${idPrefix}-month`} value={selected(entry.month)} onChange={(e) => set({ month: e.target.value })}>
        <option value="*">Every month</option>
        {MONTH_NAMES.map((name, i) => (
          <option key={name} value={String(i + 1)}>
            {name}
          </option>
        ))}
      </select>
    </Field>
  );
  const weekdaySelect = (anyLabel: string) => (
    <Field label="Weekday">
      <select
        id={`${idPrefix}-weekday`}
        value={selected(entry.day_of_week)}
        onChange={(e) => set({ day_of_week: e.target.value })}
      >
        <option value="*">{anyLabel}</option>
        {WEEKDAY_NAMES.map((name, i) => (
          <option key={name} value={String(i + 1)}>
            {name}
          </option>
        ))}
      </select>
    </Field>
  );

  return (
    <div className="bp-stack">
      <Segmented label="Entry kind" value={entry.kind} options={kinds} onChange={switchKind} />
      <FormGrid>
        {entry.kind === "date" && (
          <>
            <Field label="Year">
              <select id={`${idPrefix}-year`} value={selected(entry.year)} onChange={(e) => set({ year: e.target.value })}>
                <option value="*">Every year</option>
                {yearOptions(today, entry.year).map((year) => (
                  <option key={year} value={String(year)}>
                    {year}
                  </option>
                ))}
              </select>
            </Field>
            {monthSelect}
            <Field label="Day">
              <select id={`${idPrefix}-day`} value={selected(entry.day)} onChange={(e) => set({ day: e.target.value })}>
                <option value="*">Every day</option>
                {DAYS.map((day) => (
                  <option key={day} value={String(day)}>
                    {day}
                  </option>
                ))}
              </select>
            </Field>
            {weekdaySelect("Any weekday")}
          </>
        )}
        {entry.kind === "date-range" && (
          <>
            <Field label="Start date" help={<IsoEcho value={entry.start ?? ""} kind="date" />}>
              <input id={`${idPrefix}-start`} type="date" value={entry.start ?? ""} onChange={(e) => set({ start: e.target.value })} />
            </Field>
            <Field
              label="End date"
              error={problem === "End is before start." ? problem : null}
              help={<IsoEcho value={entry.end ?? ""} kind="date" />}
            >
              <input id={`${idPrefix}-end`} type="date" value={entry.end ?? ""} onChange={(e) => set({ end: e.target.value })} />
            </Field>
          </>
        )}
        {entry.kind === "week-and-day" && (
          <>
            {monthSelect}
            <Field label="Which week">
              <select
                id={`${idPrefix}-week`}
                value={selected(entry.week_of_month)}
                onChange={(e) => set({ week_of_month: e.target.value })}
              >
                <option value="*">Every week</option>
                {WEEK_NAMES.map((name, i) => (
                  <option key={name} value={String(i + 1)}>
                    {name}
                  </option>
                ))}
                <option value="last">Last</option>
              </select>
            </Field>
            {weekdaySelect("Every day")}
          </>
        )}
        {entry.kind === "calendar-reference" && (
          <Field label="Calendar">
            <select
              id={`${idPrefix}-calendar`}
              value={entry.instance_number ?? ""}
              onChange={(e) => set({ instance_number: e.target.value ? Number(e.target.value) : null })}
            >
              <option value="">Choose a calendar</option>
              {(calendars ?? []).map((c) => (
                <option key={c.instance_number} value={c.instance_number}>
                  {c.name}
                </option>
              ))}
            </select>
          </Field>
        )}
      </FormGrid>
      <div className="bp-match" aria-live="polite">
        <span className="bp-match__label">This entry matches</span>
        <span className="bp-match__text">{describeEntry(entry, calendarName)}</span>
        {searchable && (
          <span className="bp-match__next">{next.length ? `Next: ${next.join(" · ")}` : "No date from today on matches it."}</span>
        )}
      </div>
      {searchable && next.length === 0 && <Callout tone="warn">This entry matches no date from today on.</Callout>}
    </div>
  );
}
