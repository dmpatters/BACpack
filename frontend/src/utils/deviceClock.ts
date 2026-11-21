// Device clock display helpers.

/** "+3m 12s ahead of host", "-1h 0m 0s behind host", "in sync with host"
 *  (under a second either way -- a synced device is never exactly 0). */
export function describeOffset(seconds: number): string {
  if (Math.abs(seconds) < 1) return "in sync with host";
  let rest = Math.round(Math.abs(seconds));
  const days = Math.floor(rest / 86400);
  rest %= 86400;
  const hours = Math.floor(rest / 3600);
  rest %= 3600;
  const minutes = Math.floor(rest / 60);
  const secs = rest % 60;
  const parts: string[] = [];
  if (days) parts.push(`${days}d`);
  if (days || hours) parts.push(`${hours}h`);
  if (days || hours || minutes) parts.push(`${minutes}m`);
  parts.push(`${secs}s`);
  const ahead = seconds > 0;
  return `${ahead ? "+" : "-"}${parts.join(" ")} ${ahead ? "ahead of" : "behind"} host`;
}

/** "2026-09-25T14:03:12-04:00" -> "2026-09-25 14:03:12" */
export function wallTime(deviceTime: string): string {
  return deviceTime.slice(0, 19).replace("T", " ");
}

/** "2026-09-25T14:03:12-04:00" -> "2026-09-25T14:03:12", a datetime-local value */
export function toDatetimeLocal(deviceTime: string): string {
  return deviceTime.slice(0, 19);
}
