import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { devicesApi, logsApi } from "../api/endpoints";
import type { LogLevel } from "../api/types";
import { LoadError } from "./Banner";
import { Loading } from "./AsyncState";
import Button from "./Button";
import LogLevelSelect from "./LogLevelSelect";
import LogTable from "./LogTable";
import { collapseReattached } from "../utils/logRows";
import { matchesSearch } from "../utils/tableSort";

export const LOG_PAGE = 200;
// GET /api/logs refuses a larger limit.
export const LOG_MAX = 5000;

// The Logs page (every device, with a device filter) and a device's Logs tab
// (that device plus server-wide entries). Search runs over what is loaded.
export default function LogView({ deviceId }: { deviceId?: number }) {
  const [level, setLevel] = useState<LogLevel>("INFO");
  const [deviceFilter, setDeviceFilter] = useState<number | "all">("all");
  const [search, setSearch] = useState("");
  const [paused, setPaused] = useState(false);
  const [limit, setLimit] = useState(LOG_PAGE);

  const device = deviceId ?? (deviceFilter === "all" ? undefined : deviceFilter);
  const { data: devices = [] } = useQuery({
    queryKey: ["devices"],
    queryFn: devicesApi.list,
    enabled: deviceId === undefined,
  });
  const filterKey = ["logs", device ?? "all", level] as const;
  const { data: entries = [], error, isPending, refetch } = useQuery({
    queryKey: [...filterKey, limit],
    queryFn: () => logsApi.list(device === undefined ? { level, limit } : { device_id: device, level, limit }),
    refetchInterval: paused ? false : 5000,
    // Load older keeps the current rows on screen until the longer list
    // arrives; a filter change must not show the old filter's rows meanwhile.
    placeholderData: (previous, previousQuery) =>
      previousQuery && filterKey.every((part, i) => previousQuery.queryKey[i] === part) ? previous : undefined,
  });
  const changeFilter = <T,>(set: (value: T) => void) => (value: T) => {
    set(value);
    setLimit(LOG_PAGE);
  };

  // After Load older this is up to LOG_MAX rows: redone only when they or the search change.
  const collapsed = useMemo(() => collapseReattached(entries), [entries]);
  const rows = useMemo(
    () => collapsed.filter((r) => matchesSearch(search, r.message, r.logger_name)),
    [collapsed, search]
  );
  const hasOlder = entries.length >= limit && limit < LOG_MAX;

  return (
    <>
      <div className="bp-log-filters">
        {deviceId === undefined && (
          <div className="bp-field">
            <label className="bp-label" htmlFor="log-device-filter">
              Device
            </label>
            <select
              id="log-device-filter"
              className="bp-select"
              value={deviceFilter}
              onChange={(e) => changeFilter(setDeviceFilter)(e.target.value === "all" ? "all" : Number(e.target.value))}
            >
              <option value="all">All devices</option>
              {devices.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name} ({d.id})
                </option>
              ))}
            </select>
          </div>
        )}
        <LogLevelSelect value={level} onChange={changeFilter(setLevel)} />
        <div className="bp-field">
          <label className="bp-label" htmlFor="log-search">
            Search
          </label>
          <input
            id="log-search"
            className="bp-input"
            type="search"
            placeholder="Message or logger"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <Button type="button" aria-pressed={paused} onClick={() => setPaused((p) => !p)}>
          {paused ? "Resume updates" : "Pause updates"}
        </Button>
      </div>
      {error ? (
        <LoadError what="log entries" error={error} onRetry={() => refetch()} />
      ) : isPending ? (
        <Loading what="log entries" />
      ) : (
        <>
          <LogTable
            rows={rows}
            showDeviceColumn={device === undefined}
            emptyText={search.trim() && entries.length > 0 ? `No entries match "${search.trim()}".` : undefined}
          />
          <div className="bp-toolbar">
            <span className="bp-muted">
              Showing the newest {entries.length} {entries.length === 1 ? "entry" : "entries"}
            </span>
            <Button
              small
              type="button"
              disabled={!hasOlder}
              onClick={() => setLimit((n) => Math.min(n + LOG_PAGE, LOG_MAX))}
            >
              Load older
            </Button>
          </div>
        </>
      )}
    </>
  );
}
