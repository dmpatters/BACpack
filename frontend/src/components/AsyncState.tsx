import { useEffect, useState, type ReactNode } from "react";
import { errorMessage } from "./Banner";
import Button from "./Button";

// Under about 300 ms a spinner is only a flicker.
export function Loading({ what, delayMs = 300 }: { what: string; delayMs?: number }) {
  const [shown, setShown] = useState(delayMs <= 0);
  useEffect(() => {
    if (delayMs <= 0) return;
    const timer = setTimeout(() => setShown(true), delayMs);
    return () => clearTimeout(timer);
  }, [delayMs]);
  if (!shown) return null;
  return (
    <div className="bp-loading" role="status">
      <span className="bp-spinner" aria-hidden="true" />
      Loading {what}…
    </div>
  );
}

export function ErrorBlock({ lead, children, onRetry }: { lead: ReactNode; children?: ReactNode; onRetry?: () => void }) {
  return (
    <div className="bp-error" role="alert">
      <span>
        <strong>{lead}</strong> {children}
      </span>
      {onRetry && (
        <Button small type="button" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}

export type ActionState = { isError: boolean; isSuccess: boolean; error: unknown };

// Goes directly under the action it reports on. hideDone: the form has been
// edited since, so "Saved" would describe values no longer on screen.
export function ActionStatus({
  action,
  failed,
  done,
  hideDone = false,
}: {
  action: ActionState;
  failed: string;
  done?: ReactNode;
  hideDone?: boolean;
}) {
  if (action.isError) return <ErrorBlock lead={`${failed}:`}>{errorMessage(action.error)}</ErrorBlock>;
  if (action.isSuccess && done && !hideDone) {
    return (
      <p className="bp-done" role="status">
        {done}
      </p>
    );
  }
  return null;
}
