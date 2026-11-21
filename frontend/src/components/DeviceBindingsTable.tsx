import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { devicesApi, engineApi } from "../api/endpoints";
import { ProcessStatusPill } from "./StatusPill";
import { TableFrame } from "./DataTable";
import { formatBind } from "../utils/bind";

// Configured bindings, not just live ones: on a lab box most devices are
// stopped most of the time, so an active-only list would be empty exactly
// when someone is trying to work out what is bound where.
// defaultBind: the server's default bind address, shown for a device with none
// of its own.
export default function DeviceBindingsTable({ defaultBind }: { defaultBind?: string | null }) {
  const { data: devices = [] } = useQuery({ queryKey: ["devices"], queryFn: devicesApi.list });
  const { data: statuses = [] } = useQuery({
    queryKey: ["engineStatus"],
    queryFn: engineApi.status,
    refetchInterval: 5000,
  });

  if (statuses.length === 0) return <p className="bp-muted">No devices configured yet.</p>;

  const nameFor = (deviceId: number) =>
    devices.find((d) => d.id === deviceId)?.name ?? String(deviceId);

  return (
    <TableFrame label="Device bindings">
      <thead>
        <tr>
          <th>Device</th>
          <th>Configured bind address</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>
        {statuses.map((s) => (
          <tr key={s.device_id}>
            <td>
              <Link to={`/devices/${s.device_id}`}>{nameFor(s.device_id)}</Link>
            </td>
            {s.bind_address || defaultBind ? (
              <td className="bp-cell-mono">{formatBind(s.bind_address, defaultBind)}</td>
            ) : (
              <td>
                <span className="bp-muted">global default (below)</span>
              </td>
            )}
            <td>
              <ProcessStatusPill status={s.process_status} />
            </td>
          </tr>
        ))}
      </tbody>
    </TableFrame>
  );
}
