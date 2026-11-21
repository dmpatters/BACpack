import type { LogEntry } from "../api/types";

export type LogRow = LogEntry & { count?: number };

const REATTACHED = /^reattached to already-running device (\d+) \(pid (\d+)\)$/;
// One parent restart logs one line per surviving device, within seconds.
const SAME_RESTART_MS = 60_000;

export function collapseReattached(entries: LogEntry[]): LogRow[] {
  const rows: LogRow[] = [];
  let i = 0;
  while (i < entries.length) {
    const first = entries[i];
    if (!REATTACHED.test(first.message)) {
      rows.push(first);
      i++;
      continue;
    }
    const start = Date.parse(first.timestamp);
    let j = i + 1;
    while (
      j < entries.length &&
      REATTACHED.test(entries[j].message) &&
      Math.abs(start - Date.parse(entries[j].timestamp)) <= SAME_RESTART_MS
    ) {
      j++;
    }
    const run = entries.slice(i, j);
    if (run.length === 1) {
      rows.push(first);
    } else {
      const devices = run.map((e) => {
        const [, device, pid] = REATTACHED.exec(e.message)!;
        return `${device} (pid ${pid})`;
      });
      rows.push({
        ...first,
        device_id: null,
        message: `reattached to ${run.length} already-running devices: ${devices.join(", ")}`,
        count: run.length,
      });
    }
    i = j;
  }
  return rows;
}
