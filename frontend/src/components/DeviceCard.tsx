import { Link } from "react-router-dom";
import type { DeviceProcessStatus, DeviceSummary } from "../api/types";
import { LiveApplyPills, ProcessStatusPill } from "./StatusPill";
import Button from "./Button";
import { bindParts } from "../utils/bind";

export default function DeviceCard({
  device,
  status,
  defaultBind = null,
  busy,
  onStart,
  onStop,
}: {
  device: DeviceSummary;
  status: DeviceProcessStatus | undefined;
  defaultBind?: string | null;
  busy: boolean;
  onStart: () => void;
  onStop: () => void;
}) {
  const processStatus = status?.process_status ?? "stopped";
  const isRunning = processStatus === "running";
  // "reinitializing" is mid-flight too (a respawn), as DeviceProcessTab has it.
  const isTransitioning =
    processStatus === "starting" || processStatus === "stopping" || processStatus === "reinitializing";
  // Two rows: "address · UDP port" on one wraps on a card's width.
  const bind = bindParts(device.bind_address, defaultBind);

  return (
    // Error text stays on the device page; the card shows state, pending work
    // as pills, and one action.
    <article className="bp-device">
      <div className="bp-device__head">
        <Link className="bp-device__name" to={`/devices/${device.id}`}>
          {device.name}
        </Link>
        <span className="bp-device__state">
          <ProcessStatusPill status={processStatus} />
          <LiveApplyPills status={status} />
        </span>
      </div>
      <dl className="bp-device__meta">
        <dt>device</dt>
        <dd>{device.id}</dd>
        <dt>objects</dt>
        <dd>{device.object_count}</dd>
        <dt>pid</dt>
        <dd>{status?.process_pid ?? "—"}</dd>
        <dt>bind</dt>
        <dd>{bind.address}</dd>
        <dt>port</dt>
        <dd>{bind.port}</dd>
      </dl>
      {processStatus === "crashed" && status?.port_in_use && (
        // The free port and its Use port button are on the Process tab.
        <p className="bp-help">
          <Link to={`/devices/${device.id}/process`}>Port in use: pick another</Link>
        </p>
      )}
      <div className="bp-device__actions">
        {isRunning || isTransitioning ? (
          <Button small disabled={busy || isTransitioning} onClick={onStop}>
            Stop
          </Button>
        ) : (
          <Button variant="primary" small disabled={busy} onClick={onStart}>
            Start
          </Button>
        )}
      </div>
    </article>
  );
}
