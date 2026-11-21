import { Fragment, useId, useState, type CSSProperties } from "react";
import type { ScheduleDaySchedule, ScheduleTimeValue } from "../api/types";
import { WEEKDAY_NAMES, WEEKDAY_SHORT } from "../utils/calendarDates";
import {
  copyDay,
  daySegments,
  daySummary,
  defaultName,
  duplicateTimes,
  firstOtherValue,
  nextEntryTime,
  nowMarker,
  weekTones,
  type ValueNaming,
} from "../utils/scheduleGrid";
import Button from "./Button";
import ValueControl from "./ValueControl";
import IsoEcho from "./IsoEcho";

const pct = (fraction: number) => `${(fraction * 100).toFixed(3)}%`;
const position = (vars: Record<string, string>) => vars as CSSProperties;
const ALL_DAYS = [0, 1, 2, 3, 4, 5, 6];
const WEEKDAYS = [0, 1, 2, 3, 4];

function joinDays(days: number[]): string {
  const names = days.map((d) => WEEKDAY_NAMES[d]);
  return names.length === 1 ? names[0] : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

// docs/design-system/components/ScheduleGrid. Times are the device's own.
export default function ScheduleGrid({
  days,
  onChange,
  naming,
  deviceTime,
}: {
  days: ScheduleDaySchedule[];
  onChange: (days: ScheduleDaySchedule[]) => void;
  naming: ValueNaming;
  deviceTime?: string | null;
}) {
  const copyLabelId = useId();
  const [selected, setSelected] = useState(0);
  const [copyTo, setCopyTo] = useState<number[]>([]);
  const [copied, setCopied] = useState<string | null>(null);

  const tones = weekTones(days, naming);
  const now = deviceTime ? nowMarker(deviceTime) : null;
  const entries = days[selected] ?? [];
  const dupes = duplicateTimes(entries);
  const dayName = WEEKDAY_NAMES[selected];
  const fallback = defaultName(naming);

  const setEntries = (next: ScheduleTimeValue[]) => {
    setCopied(null);
    onChange(days.map((day, i) => (i === selected ? next : day)));
  };
  const select = (day: number) => {
    setSelected(day);
    setCopyTo([]);
    setCopied(null);
  };
  const others = (list: number[]) => list.filter((d) => d !== selected);

  return (
    <div className="bp-stack">
      <div className="bp-sched" role="group" aria-label="Weekly schedule, device time">
        <div className="bp-sched__axis">
          <span />
          <div className="bp-sched__ticks" aria-hidden="true">
            <span>00</span>
            <span>06</span>
            <span>12</span>
            <span>18</span>
            <span>24</span>
          </div>
          <span className="bp-sched__sum">Device time{now ? ` · now ${now.label}` : ""}</span>
        </div>
        {days.map((day, i) => (
          <div className="bp-sched__row" key={i} aria-current={i === selected ? "true" : undefined}>
            <button
              type="button"
              className="bp-sched__day"
              aria-pressed={i === selected}
              aria-label={WEEKDAY_NAMES[i]}
              onClick={() => select(i)}
            >
              {WEEKDAY_SHORT[i]}
            </button>
            <div className="bp-sched__track">
              {daySegments(day, naming, tones).map((segment, k) => (
                <span
                  key={k}
                  className={segment.tone ? `bp-sched__seg bp-sched__seg--${segment.tone}` : "bp-sched__seg"}
                  style={position({ "--start": pct(segment.start), "--span": pct(segment.span) })}
                >
                  {segment.label}
                </span>
              ))}
              {now && now.day === i && (
                <span className="bp-sched__now" aria-hidden="true" style={position({ "--at": pct(now.at) })} />
              )}
            </div>
            <span className="bp-sched__sum">{daySummary(day, naming)}</span>
          </div>
        ))}
      </div>

      <div className="bp-card bp-sched__edit">
        <h3 className="bp-section-title">{dayName}</h3>
        {entries.length === 0 && <p className="bp-help">No entries: {fallback} all day.</p>}
        {naming.family === "analog" && naming.unit && <p className="bp-help">Values in {naming.unit}.</p>}
        {entries.map((tv, k) => (
          <Fragment key={k}>
            <div className="bp-sched__entry">
              <input
                className="bp-input"
                type="time"
                aria-label={`Time, entry ${k + 1}`}
                aria-invalid={dupes.has(k) ? true : undefined}
                value={tv.time}
                onChange={(e) => setEntries(entries.map((x, j) => (j === k ? { ...x, time: e.target.value } : x)))}
              />
              <IsoEcho value={tv.time} kind="time" zone="device time" />
              <ValueControl
                aria-label={`Value, entry ${k + 1}`}
                family={naming.family}
                states={naming.states}
                emptyLabel={`Default (${fallback})`}
                value={tv.value ?? ""}
                onChange={(value) => setEntries(entries.map((x, j) => (j === k ? { ...x, value } : x)))}
              />
              <Button
                variant="delete"
                small
                type="button"
                aria-label={`Delete entry ${k + 1}`}
                onClick={() => setEntries(entries.filter((_, j) => j !== k))}
              >
                Delete
              </Button>
            </div>
            {dupes.has(k) && (
              <p className="bp-field-error" role="alert">
                Entry {k + 1} repeats {tv.time}.
              </p>
            )}
          </Fragment>
        ))}
        <div>
          <Button
            small
            type="button"
            onClick={() => setEntries([...entries, { time: nextEntryTime(entries), value: firstOtherValue(naming) }])}
          >
            Add entry
          </Button>
        </div>
        <div className="bp-field">
          <span className="bp-label" id={copyLabelId}>
            Copy {dayName} to
          </span>
          <div className="bp-cluster" role="group" aria-labelledby={copyLabelId}>
            <span className="bp-daypick">
              {ALL_DAYS.filter((d) => d !== selected).map((d) => (
                <button
                  key={d}
                  type="button"
                  aria-label={`Copy to ${WEEKDAY_NAMES[d]}`}
                  aria-pressed={copyTo.includes(d)}
                  onClick={() => setCopyTo((list) => (list.includes(d) ? list.filter((x) => x !== d) : [...list, d]))}
                >
                  {WEEKDAY_SHORT[d]}
                </button>
              ))}
            </span>
            <Button variant="ghost" small type="button" onClick={() => setCopyTo(others(WEEKDAYS))}>
              Weekdays
            </Button>
            <Button variant="ghost" small type="button" onClick={() => setCopyTo(others(ALL_DAYS))}>
              All days
            </Button>
            <Button
              variant="primary"
              small
              type="button"
              disabled={copyTo.length === 0}
              onClick={() => {
                const targets = [...copyTo].sort((a, b) => a - b);
                onChange(copyDay(days, selected, targets));
                setCopied(`Copied ${dayName} to ${joinDays(targets)}.`);
                setCopyTo([]);
              }}
            >
              Copy
            </Button>
          </div>
          <span className="bp-help">Copy replaces the chosen days' entries.</span>
        </div>
        {copied && (
          <p className="bp-done" role="status">
            {copied}
          </p>
        )}
      </div>
    </div>
  );
}
