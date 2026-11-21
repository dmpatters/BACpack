import type { ReactNode } from "react";
import type { DeviceProcessStatus, LogLevel, ProcessStatus } from "../api/types";
import { EmptyValue } from "./DataTable";

export type PillTone = "ok" | "warn" | "danger" | "info";

export default function StatusPill({
  tone,
  plain = false,
  live = false,
  children,
}: {
  tone?: PillTone;
  plain?: boolean;
  live?: boolean;
  children: ReactNode;
}) {
  const classes = ["bp-pill"];
  if (tone) classes.push(`bp-pill--${tone}`);
  if (plain) classes.push("bp-pill--plain");
  return (
    <span className={classes.join(" ")} aria-live={live ? "polite" : undefined}>
      {children}
    </span>
  );
}

// Transitional states stay neutral: they resolve on their own into one of the
// coloured end states.
const PROCESS: Record<ProcessStatus, { text: string; tone?: PillTone; live?: boolean }> = {
  running: { text: "running", tone: "ok" },
  stopped: { text: "stopped" },
  crashed: { text: "crashed", tone: "danger" },
  starting: { text: "starting…", live: true },
  stopping: { text: "stopping…", live: true },
  reinitializing: { text: "reinitializing…", live: true },
};

export function ProcessStatusPill({ status }: { status: ProcessStatus }) {
  const { text, tone, live } = PROCESS[status];
  return (
    <StatusPill tone={tone} live={live}>
      {text}
    </StatusPill>
  );
}

export function ResultPill({ error }: { error: string | null | undefined }) {
  if (error === undefined) return <EmptyValue />;
  return error === null ? <StatusPill tone="ok">ok</StatusPill> : <StatusPill tone="danger">error</StatusPill>;
}

const LEVEL_TONE: Record<LogLevel, PillTone | undefined> = {
  DEBUG: undefined,
  INFO: "info",
  WARNING: "warn",
  ERROR: "danger",
  CRITICAL: "danger",
};

export function LogLevelPill({ level }: { level: LogLevel }) {
  return (
    <StatusPill tone={LEVEL_TONE[level]} plain>
      {level}
    </StatusPill>
  );
}

// Pending work on a device card; the reasons and "Restart serving" live on
// the device page.
export function LiveApplyPills({ status }: { status?: Pick<DeviceProcessStatus, "restart_pending" | "apply_error"> }) {
  return (
    <>
      {status?.restart_pending && <StatusPill tone="warn">restart pending</StatusPill>}
      {status?.apply_error && <StatusPill tone="danger">apply failed</StatusPill>}
    </>
  );
}
