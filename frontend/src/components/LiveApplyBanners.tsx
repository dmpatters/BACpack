import type { DeviceProcessStatus } from "../api/types";
import Callout from "./Callout";

// Live object edits: object edits apply to a running device by themselves;
// these say when one couldn't, or when saved device settings still need a
// restart.
export default function LiveApplyBanners({ status }: { status: DeviceProcessStatus | undefined }) {
  if (!status || status.process_status !== "running") return null;
  return (
    <>
      {status.apply_error && (
        <Callout tone="danger" data-testid="apply-error">
          The running device couldn't apply the latest changes: {status.apply_error}. It keeps serving the
          previous configuration.
        </Callout>
      )}
      {status.restart_pending && (
        <Callout tone="warn" data-testid="restart-pending">
          Device settings changed. Restart serving to apply them.
        </Callout>
      )}
    </>
  );
}
