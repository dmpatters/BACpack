import { describe, expect, it } from "vitest";
import type { LogEntry } from "../api/types";
import { collapseReattached } from "./logRows";

let nextId = 1;
const entry = (timestamp: string, message: string, device_id: number | null = null): LogEntry => ({
  id: nextId++,
  timestamp,
  level: "INFO",
  logger_name: "bacpack.engine",
  message,
  device_id,
});

describe("collapseReattached", () => {
  it("folds one restart's reattach lines into a single row with a count", () => {
    const rows = collapseReattached([
      entry("2026-10-13T10:00:05Z", "web UI started"),
      entry("2026-10-13T10:00:02Z", "reattached to already-running device 599002 (pid 11680)", 599002),
      entry("2026-10-13T10:00:01Z", "reattached to already-running device 599001 (pid 11679)", 599001),
      entry("2026-10-13T09:00:00Z", "device 599001 started", 599001),
    ]);
    expect(rows.map((r) => r.message)).toEqual([
      "web UI started",
      "reattached to 2 already-running devices: 599002 (pid 11680), 599001 (pid 11679)",
      "device 599001 started",
    ]);
    expect(rows[1]).toMatchObject({ count: 2, device_id: null, timestamp: "2026-10-13T10:00:02Z" });
  });

  it("keeps two restarts apart, and leaves a single reattach alone", () => {
    const rows = collapseReattached([
      entry("2026-10-13T12:00:00Z", "reattached to already-running device 599001 (pid 2)", 599001),
      entry("2026-10-13T10:00:00Z", "reattached to already-running device 599001 (pid 1)", 599001),
    ]);
    expect(rows).toHaveLength(2);
    expect(rows[0].count).toBeUndefined();
    expect(rows[0].device_id).toBe(599001);
  });
});
