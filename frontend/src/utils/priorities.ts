import type { LiveObjectDTO } from "../api/types";

// The priorities ASHRAE 135 Table 19-1 names; the rest are just numbers.
export const PRIORITY_NAMES: Readonly<Record<number, string>> = {
  1: "Manual life safety",
  2: "Automatic life safety",
  5: "Critical equipment control",
  6: "Minimum on/off",
  8: "Manual operator",
};

// Where a commandable object's Present_Value comes from, or null for an
// object with no priority array.
export function controlledFrom(live: Pick<LiveObjectDTO, "priority_array" | "controlling_priority">): string | null {
  if (!live.priority_array) return null;
  if (live.controlling_priority === null) return "from Relinquish default";
  return `from priority ${live.controlling_priority}`;
}
