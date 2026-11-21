import type { LogLevel } from "../api/types";

// GET /api/logs filters with `>= min_level` (see get_logs in web/api/diagnostics.py), so
// every choice here is a threshold, not an exact match. The wording says so
// out loud: a bare "INFO" reads as "show me INFO", which is what made the
// device Logs tab and the server-wide Logs page look like they disagreed
// with each other. CRITICAL is the top of the scale, so "and above" would be
// a lie there.
const LEVEL_LABELS: Array<[LogLevel, string]> = [
  ["INFO", "INFO and above"],
  ["WARNING", "WARNING and above"],
  ["ERROR", "ERROR and above"],
  ["CRITICAL", "CRITICAL only"],
];

export default function LogLevelSelect({
  value,
  onChange,
}: {
  value: LogLevel;
  onChange: (level: LogLevel) => void;
}) {
  return (
    <div className="bp-field">
      <label className="bp-label" htmlFor="log-level">
        Minimum level
      </label>
      <select
        id="log-level"
        className="bp-select"
        value={value}
        onChange={(e) => onChange(e.target.value as LogLevel)}
      >
        {LEVEL_LABELS.map(([level, label]) => (
          <option key={level} value={level}>
            {label}
          </option>
        ))}
      </select>
    </div>
  );
}
