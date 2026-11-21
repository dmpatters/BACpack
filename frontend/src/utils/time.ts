// The API's timestamps are ISO 8601 UTC ("2026-09-30T00:12:53+00:00"); a
// few come back with no zone at all (SQLite drops it -- process_started_at)
// but are UTC too. Every page shows them the same way, still in UTC, since
// the devices' own clocks and the box's logs are what they get compared to.
// "YYYY-MM-DD HH:MM:SS" in UTC, or null for text that isn't a time.
function utcDisplay(iso: string): string | null {
  const hasZone = /(Z|[+-]\d{2}:?\d{2})$/i.test(iso);
  // Date only promises to parse milliseconds, not Python's microseconds.
  const trimmed = iso.replace(/(\.\d{3})\d+/, "$1");
  const date = new Date(hasZone ? trimmed : `${trimmed}Z`);
  if (Number.isNaN(date.getTime())) return null;
  return date.toISOString().slice(0, 19).replace("T", " ");
}

export function formatUtcBare(iso: string): string {
  return utcDisplay(iso) ?? iso;
}

export function formatUtc(iso: string): string {
  const shown = utcDisplay(iso);
  return shown === null ? iso : `${shown} UTC`;
}

// "10:48:02 UTC": a live reading's time of day.
export function formatUtcTime(iso: string): string {
  const shown = utcDisplay(iso);
  return shown === null ? iso : `${shown.slice(11)} UTC`;
}
