// Canonical list of types with intrinsic event reporting -- the single
// source of truth for which object types are alarming-capable. Binary
// Output and Multi-state Output are deliberately absent: they would need
// the Command Failure algorithm, which bacpypes3 doesn't implement.
export const ALARMING_CAPABLE_TYPES = [
  "analog-input",
  "analog-output",
  "analog-value",
  "binary-input",
  "binary-value",
  "multi-state-input",
  "multi-state-value",
] as const;

export function isAlarmingCapable(objectType: string): boolean {
  return (ALARMING_CAPABLE_TYPES as readonly string[]).includes(objectType);
}

// Mirrors _ANALOG_TYPES / _COMMANDABLE_TYPES in models.py: the API ignores
// units on anything else, and relinquish_default on anything without a
// priority array, so the General tab only shows them where they apply.
export const ANALOG_TYPES = ["analog-input", "analog-output", "analog-value"];
const COMMANDABLE_TYPES = [
  "analog-output",
  "analog-value",
  "binary-output",
  "binary-value",
  "multi-state-output",
  "multi-state-value",
];

export function hasUnits(objectType: string, scheduleValueType?: string | null): boolean {
  return ANALOG_TYPES.includes(objectType) || (objectType === "schedule" && scheduleValueType === "analog");
}

export function isCommandable(objectType: string): boolean {
  return COMMANDABLE_TYPES.includes(objectType);
}

export function hasRelinquishDefault(objectType: string): boolean {
  return isCommandable(objectType);
}

// Mirrors models.INPUT_TYPES: the types with a saved start value.
export function isInput(objectType: string): boolean {
  return objectType === "analog-input" || objectType === "binary-input" || objectType === "multi-state-input";
}

// What `bacnet_writable` closes depends on the type (bacnet.py's
// BacnetWriteLocked, the schedule/calendar allowlists): a point keeps its
// other properties writable, which a user reading "read-only" wouldn't expect.
export function bacnetWritableHelp(objectType: string): string {
  if (objectType === "schedule") {
    return "Lets a BACnet client configure this schedule.";
  }
  if (objectType === "calendar") {
    return "Lets a BACnet client configure this calendar.";
  }
  if (!isCommandable(objectType)) return "No effect on this object type.";
  return "Unchecked, BACnet clients can't command Present_Value.";
}

// Standard types with no Present_Value a schedule could write (and schedule
// and calendar, whose Present_Value is calculated). A denylist, not an
// allowlist, so a remote device's other types -- loop, accumulator,
// integer-value, proprietary numbers -- are still offered.
const NO_WRITABLE_PRESENT_VALUE = [
  "averaging",
  "calendar",
  "device",
  "event-enrollment",
  "event-log",
  "file",
  "network-port",
  "notification-class",
  "notification-forwarder",
  "program",
  "schedule",
  "structured-view",
  "trend-log",
  "trend-log-multiple",
];

export function canBeScheduleWriteTarget(objectType: string): boolean {
  return !NO_WRITABLE_PRESENT_VALUE.includes(objectType);
}

export type ValueFamily = "analog" | "binary" | "multi-state";

// Why a stored value can't be used by this family, or null ("" is no value).
// A value survives a type change in the forms, so it's checked before saving.
export function valueProblem(value: string, family: ValueFamily, stateCount: number): string | null {
  if (value.trim() === "") return null;
  if (family === "analog") return Number.isFinite(Number(value)) ? null : `${value} is not a number`;
  if (family === "binary") return value === "active" || value === "inactive" ? null : `${value} is not a binary value`;
  const n = Number(value);
  return Number.isInteger(n) && n >= 1 && n <= stateCount ? null : `${value} is not a state`;
}

export function valueFamily(objectType: string): ValueFamily | null {
  if (objectType.startsWith("analog-")) return "analog";
  if (objectType.startsWith("binary-")) return "binary";
  if (objectType.startsWith("multi-state-")) return "multi-state";
  return null;
}
