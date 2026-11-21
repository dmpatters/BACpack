// Mirrors bacpack.models.OBJECT_TYPES exactly -- kebab-case, matching what's
// stored in SQLite and returned by the JSON API.
export const OBJECT_TYPES = [
  "analog-input",
  "analog-output",
  "analog-value",
  "binary-input",
  "binary-output",
  "binary-value",
  "multi-state-input",
  "multi-state-output",
  "multi-state-value",
  "schedule",
  "calendar",
  "notification-class",
] as const;

// Mirrors trending.BYTES_PER_TREND_SAMPLE (src/bacpack/trending.py) -- keep
// both in sync if that empirical constant is ever recomputed.
export const BYTES_PER_TREND_SAMPLE = 105;

// Mirrors models.MAX_TREND_SAMPLE_CAP (src/bacpack/models.py): the largest
// sample cap, and the cap of a trend without one.
export const MAX_TREND_SAMPLE_CAP = 32767;

// Mirrors trending.py's fallback for an interval-mode trend with no interval.
export const DEFAULT_TREND_INTERVAL_SECONDS = 60;

// Mirrors bacnet.FEED_PRIORITY (src/bacpack/bacnet.py): the slot connectors write.
export const FEED_PRIORITY = 10;

// Same refresh as the Logs page.
export const LIVE_POLL_MS = 5000;
