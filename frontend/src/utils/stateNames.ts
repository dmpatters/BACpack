import type { BacnetObjectDTO } from "../api/types";

// docs/design-system/components/StateEditor. A multi-state object's states
// are numbered by list position, as in BACnet.

export const MAX_STATES = 254;

export const STATE_PRESETS: { label: string; names: string[] }[] = [
  { label: "Off / On", names: ["Off", "On"] },
  { label: "Off / Low / High", names: ["Off", "Low", "High"] },
  { label: "Occupied / Standby / Unoccupied", names: ["Occupied", "Standby", "Unoccupied"] },
];

// An edit to the list, and where each old state number went:
// renumber[n - 1] is old state n's new number, or null once it's deleted.
export interface StateEdit {
  names: string[];
  renumber: (number | null)[];
}

const unchanged = (count: number) => Array.from({ length: count }, (_, i) => i + 1);

export function renameState(names: string[], index: number, name: string): StateEdit {
  return { names: names.map((old, i) => (i === index ? name : old)), renumber: unchanged(names.length) };
}

export function addState(names: string[]): StateEdit {
  return { names: [...names, `State ${names.length + 1}`], renumber: unchanged(names.length) };
}

export function moveState(names: string[], index: number, delta: -1 | 1): StateEdit {
  const to = index + delta;
  const renumber = unchanged(names.length);
  if (to < 0 || to >= names.length) return { names, renumber };
  const next = [...names];
  [next[index], next[to]] = [next[to], next[index]];
  renumber[index] = to + 1;
  renumber[to] = index + 1;
  return { names: next, renumber };
}

export function deleteState(names: string[], index: number): StateEdit {
  return {
    names: names.filter((_, i) => i !== index),
    renumber: names.map((_, i) => (i === index ? null : i < index ? i + 1 : i)),
  };
}

// A preset only fills the list; a state number it still has keeps that number.
export function presetStates(names: string[], preset: string[]): StateEdit {
  return { names: [...preset], renumber: names.map((_, i) => (i < preset.length ? i + 1 : null)) };
}

export function renumberValue(value: string, renumber: (number | null)[]): string {
  const n = Number(value);
  if (value.trim() === "" || !Number.isInteger(n) || n < 1 || n > renumber.length) return value;
  const moved = renumber[n - 1];
  return moved === null ? "" : String(moved);
}

export function trackOrigins(origins: (number | null)[], edit: StateEdit): (number | null)[] {
  const next: (number | null)[] = edit.names.map(() => null);
  edit.renumber.forEach((to, i) => {
    if (to !== null) next[to - 1] = origins[i] ?? null;
  });
  return next;
}

export function stateCountText(count: number): string {
  return count === 1 ? "1 state" : `${count} states`;
}

export function stateOptionLabel(number: number, name: string | undefined): string {
  return name && name.trim() ? `${number} · ${name}` : `${number} · (no name)`;
}

// Names are required and unique (the backend takes anything, since a BACnet
// client may write State_Text; this editor doesn't save such a list).
export function stateProblems(names: string[]): { message: string | null; rows: Map<number, string> } {
  const rows = new Map<number, string>();
  const seen = new Map<string, number>();
  names.forEach((name, i) => {
    const key = name.trim().toLowerCase();
    if (!key) {
      rows.set(i, `State ${i + 1} needs a name.`);
      return;
    }
    const first = seen.get(key);
    if (first === undefined) seen.set(key, i);
    else rows.set(i, `State ${i + 1} repeats the name of state ${first + 1}.`);
  });
  const message = names.length === 0 ? "Add at least one state." : (rows.values().next().value ?? null);
  return { message, rows };
}

export interface StateReference {
  who: string;
  states: number[];
}

const uniqueSorted = (numbers: number[]) => [...new Set(numbers)].sort((a, b) => a - b);

export function scheduleStates(schedule: BacnetObjectDTO): number[] {
  const values = [
    schedule.schedule_default,
    ...(schedule.weekly_schedule ?? []).flat().map((tv) => tv.value),
    ...(schedule.exception_schedule ?? []).flatMap((e) => e.time_values.map((tv) => tv.value)),
  ];
  return uniqueSorted(values.map(Number).filter((n) => Number.isInteger(n) && n >= 1));
}

// What else holds this object's state numbers: local multi-state schedules
// writing it, and its own alarm and simulation settings. Renumbering the
// states doesn't update any of them.
export function stateReferences(obj: BacnetObjectDTO, siblings: BacnetObjectDTO[]): StateReference[] {
  const refs: StateReference[] = [];
  for (const other of siblings) {
    if (other.object_type !== "schedule" || other.schedule_value_type !== "multi-state") continue;
    const writesThis = (other.schedule_write_targets ?? []).some(
      (t) => t.device_instance == null && t.object_type === obj.object_type && t.instance_number === obj.instance_number
    );
    const states = writesThis ? scheduleStates(other) : [];
    if (states.length) refs.push({ who: `Schedule ${other.name}`, states });
  }
  if (obj.alarm_values?.length) refs.push({ who: "Alarming on this object", states: uniqueSorted(obj.alarm_values) });
  const simulated = obj.simulation?.states;
  if (Array.isArray(simulated) && simulated.length) {
    refs.push({ who: "Simulation on this object", states: uniqueSorted(simulated) });
  }
  return refs;
}

export function referenceText(ref: StateReference, names: string[]): string {
  const named = ref.states.map((n) => (names[n - 1]?.trim() ? `${n} (${names[n - 1]})` : String(n)));
  const list = named.length === 1 ? named[0] : `${named.slice(0, -1).join(", ")} and ${named[named.length - 1]}`;
  return `${ref.who} uses ${named.length === 1 ? "state" : "states"} ${list}.`;
}
