import type { HTMLAttributes, ReactNode } from "react";

export type CalloutTone = "info" | "ok" | "warn" | "danger";

const ROLES: Record<CalloutTone, string> = { info: "note", ok: "status", warn: "alert", danger: "alert" };

export default function Callout({
  tone,
  lead,
  children,
  ...props
}: { tone: CalloutTone; lead?: ReactNode; children?: ReactNode } & HTMLAttributes<HTMLDivElement>) {
  return (
    <div {...props} className={`bp-callout bp-callout--${tone}`} role={ROLES[tone]}>
      {lead && <strong>{lead}</strong>} {children}
    </div>
  );
}
