import type { LiveObjectDTO } from "../api/types";
import { FEED_PRIORITY } from "../constants";

// A read mapping writes FEED_PRIORITY, so that slot shows what it last wrote
// even under a higher-priority override; a proxy mirrors the remote's value.
export function mappingValue(live: LiveObjectDTO | undefined, direction: "read" | "proxy"): unknown {
  if (live === undefined) return undefined;
  if (direction === "proxy" || live.priority_array === null) return live.present_value;
  return live.priority_array[FEED_PRIORITY - 1];
}
