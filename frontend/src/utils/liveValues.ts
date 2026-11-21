import type { BacnetObjectDTO } from "../api/types";
import type { PillTone } from "../components/StatusPill";
import { valueFamily } from "./objectTypes";

type ValueOwner = Pick<BacnetObjectDTO, "object_type" | "state_text" | "schedule_value_type">;

// A live value as the UI names it: the same words the editors use.
export function formatLiveValue(value: unknown, obj: ValueOwner): string {
  if (value === null || value === undefined) return "—";
  if (obj.object_type === "calendar") return value ? "In calendar" : "Not in calendar";
  const family = obj.object_type === "schedule" ? obj.schedule_value_type : valueFamily(obj.object_type);
  if (family === "binary") return value === "active" ? "Active" : value === "inactive" ? "Inactive" : String(value);
  if (family === "multi-state" && typeof value === "number") {
    const states = obj.state_text ?? [];
    // A multi-state schedule may have no state names at all.
    if (states.length === 0) return `${value} · State ${value}`;
    if (value < 1 || value > states.length) return `${value} · not a state`;
    return `${value} · ${states[value - 1]?.trim() || `State ${value}`}`;
  }
  if (typeof value === "number") return String(Number(value.toFixed(2)));
  return String(value);
}

export function flagTone(flag: string): PillTone | undefined {
  if (flag === "in-alarm" || flag === "fault") return "danger";
  if (flag === "overridden") return "warn";
  return undefined;
}

export function eventTone(state: string): PillTone {
  return state === "normal" ? "ok" : "danger";
}
