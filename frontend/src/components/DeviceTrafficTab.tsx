import { useQuery } from "@tanstack/react-query";
import { engineApi, trafficApi } from "../api/endpoints";
import { formatUtcBare } from "../utils/time";
import { LoadError } from "./Banner";
import { TableFrame } from "./DataTable";

export default function DeviceTrafficTab({ deviceId }: { deviceId: number }) {
  const { data: statuses = [] } = useQuery({
    queryKey: ["engineStatus"],
    queryFn: engineApi.status,
    refetchInterval: 5000,
  });
  const isRunning = statuses.find((s) => s.device_id === deviceId)?.process_status === "running";

  const { data: entries = [], error } = useQuery({
    queryKey: ["traffic", deviceId],
    queryFn: () => trafficApi.recent(deviceId),
    refetchInterval: 2000,
  });

  return (
    <>
      <p className="bp-muted">
        Requests received, schedule writes and requests sent as a client. Ordinary reads and writes aren't recorded.
        {!isRunning && (
          <>
            <br />
            <strong>This device is not currently live.</strong>
          </>
        )}
      </p>

      {error ? (
        <LoadError what="traffic" error={error} />
      ) : entries.length > 0 ? (
        <TableFrame label="Traffic" scroll>
          <thead>
            <tr>
              <th>Time (UTC)</th>
              <th>Event</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((e, i) => (
              <tr key={i}>
                <td className="bp-cell-time">{formatUtcBare(e.timestamp)}</td>
                <td className="bp-cell-mono">{e.event_type}</td>
                <td className="bp-cell-msg">{e.detail}</td>
              </tr>
            ))}
          </tbody>
        </TableFrame>
      ) : (
        <p className="bp-muted">No traffic recorded yet.</p>
      )}
    </>
  );
}
