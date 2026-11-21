import { useQueries, useQuery } from "@tanstack/react-query";
import { ApiError } from "../api/http";
import { liveApi } from "../api/endpoints";
import type { LiveObjectDTO, ObjectLiveDTO } from "../api/types";
import { LIVE_POLL_MS } from "../constants";

export type LiveStatus = "loading" | "live" | "stale" | "stopped" | "outdated" | "missing";

export type ObjectLive =
  | { status: "loading"; data?: undefined; message?: undefined }
  | { status: "live" | "stale"; data: ObjectLiveDTO; message?: undefined }
  | { status: "stale" | "stopped" | "outdated" | "missing"; data?: undefined; message: string };

// The API's 409s differ only in wording (web/api/live.py).
export function classifyLiveError(error: unknown): Exclude<LiveStatus, "loading" | "live"> {
  if (error instanceof ApiError) {
    if (error.status === 409) return /restart the device/.test(error.message) ? "outdated" : "stopped";
    if (error.status === 404) return "missing";
  }
  return "stale";
}

// An older device process won't answer until it is restarted, and each
// request makes it log a traceback: ask again only now and then, so the
// readout recovers once the user restarts it.
const OUTDATED_RETRY_MS = 30_000;

function pollInterval(error: unknown): number {
  return error && classifyLiveError(error) === "outdated" ? OUTDATED_RETRY_MS : LIVE_POLL_MS;
}

export function useObjectLive(deviceId: number, objectId: number | null, enabled = true): ObjectLive {
  const query = useQuery({
    queryKey: ["live", deviceId, objectId],
    queryFn: () => liveApi.object(deviceId, objectId!),
    enabled: enabled && objectId !== null,
    retry: false,
    refetchInterval: (q) => pollInterval(q.state.error),
  });
  if (query.error) {
    const kind = classifyLiveError(query.error);
    // Not running, missing and outdated drop the old values: they aren't
    // what the device serves now. A failed poll keeps them, marked stale.
    if (kind === "stale" && query.data) return { status: "stale", data: query.data };
    return { status: kind, message: (query.error as Error).message };
  }
  if (query.data) return { status: "live", data: query.data };
  return { status: "loading" };
}

export function useDeviceLives(deviceIds: number[]): Map<number, LiveObjectDTO> {
  const results = useQueries({
    queries: deviceIds.map((id) => ({
      queryKey: ["live", id],
      queryFn: () => liveApi.device(id),
      retry: false,
      refetchInterval: (q: { state: { error: unknown } }) => pollInterval(q.state.error),
    })),
  });
  const byObject = new Map<number, LiveObjectDTO>();
  for (const result of results) {
    // A table cell has no room to say "stale": after a failed poll it shows
    // nothing rather than an old value that looks current.
    if (result.error) continue;
    for (const entry of result.data?.objects ?? []) byObject.set(entry.object_id, entry);
  }
  return byObject;
}

export function ageText(readAt: string, nowMs: number): string {
  const seconds = Math.max(0, Math.round((nowMs - Date.parse(readAt)) / 1000));
  if (seconds < 60) return `${seconds} s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  return `${Math.round(seconds / 3600)} h ago`;
}
