import type { ConnectorStatusDTO } from "../api/types";
import StatusPill from "./StatusPill";

export type ConnectorHealth = "not running" | "suppressed" | "failing" | "ok" | "waiting";

// One summary for both connector types. A bacnet connector with only write
// mappings never polls, so its write results count too.
export function connectorHealth(status?: ConnectorStatusDTO): ConnectorHealth {
  if (!status) return "not running";
  if (status.suppressed_by_dcc) return "suppressed";
  const writes = Object.values(status.write_status ?? {});
  if (status.last_success === false || writes.some((w) => w.error)) return "failing";
  if (status.last_success === true || writes.length > 0) return "ok";
  return "waiting";
}

export default function ConnectorHealthPill({ status, enabled = true }: { status?: ConnectorStatusDTO; enabled?: boolean }) {
  if (!enabled) return <StatusPill>disabled</StatusPill>;
  switch (connectorHealth(status)) {
    case "not running":
      return <StatusPill>not running</StatusPill>;
    case "suppressed":
      return <StatusPill tone="warn">suppressed by DCC</StatusPill>;
    case "failing":
      return <StatusPill tone="danger">failing ({status?.consecutive_failures ?? 0})</StatusPill>;
    case "ok":
      return <StatusPill tone="ok">ok</StatusPill>;
    default:
      return <StatusPill live>waiting…</StatusPill>;
  }
}
