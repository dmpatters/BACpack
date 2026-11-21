import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import Button from "./Button";

export type ConfirmRequest = {
  title: string;
  body: ReactNode;
  confirmLabel: string;
  onConfirm: () => Promise<unknown> | void;
};

// One dialog per page for "are you sure" deletes. Busy while the action runs;
// closes when it settles either way, since the caller's mutation reports errors.
export function useConfirm(): { ask: (request: ConfirmRequest) => void; dialog: ReactNode } {
  const [request, setRequest] = useState<ConfirmRequest | null>(null);
  const [busy, setBusy] = useState(false);
  const close = () => {
    setRequest(null);
    setBusy(false);
  };
  const dialog = request && (
    <ConfirmDialog
      title={request.title}
      confirmLabel={request.confirmLabel}
      busy={busy}
      onCancel={close}
      onConfirm={() => {
        setBusy(true);
        Promise.resolve()
          .then(request.onConfirm)
          .then(close, close);
      }}
    >
      {request.body}
    </ConfirmDialog>
  );
  return { ask: setRequest, dialog };
}

export default function ConfirmDialog({
  title,
  children,
  confirmLabel,
  busy = false,
  onConfirm,
  onCancel,
}: {
  title: string;
  children: ReactNode;
  confirmLabel: string;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const titleId = useId();
  const textId = useId();
  const cancelRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);

  // Focus starts on Cancel, the safe choice, and goes back to whatever opened
  // the dialog when it closes.
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    cancelRef.current?.focus();
    return () => opener?.focus();
  }, []);

  // Both buttons are disabled while the action runs; without this, focus
  // would fall to the page behind the scrim.
  useEffect(() => {
    if (busy) dialogRef.current?.focus();
  }, [busy]);

  // Once the action is under way, closing the dialog would look like an abort
  // that didn't happen.
  const cancel = () => {
    if (!busy) onCancel();
  };

  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      cancel();
      return;
    }
    if (e.key !== "Tab") return;
    e.preventDefault();
    const order = [cancelRef.current, confirmRef.current].filter(
      (el): el is HTMLButtonElement => el !== null && !el.disabled
    );
    if (order.length === 0) return;
    const index = order.indexOf(document.activeElement as HTMLButtonElement);
    const next = e.shiftKey ? (index <= 0 ? order.length - 1 : index - 1) : (index + 1) % order.length;
    order[next].focus();
  };

  return (
    // mousedown, not click: a text selection dragged out of the dialog
    // shouldn't close it.
    <div
      className="bp-scrim"
      data-testid="confirm-scrim"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) cancel();
      }}
      onKeyDown={onKeyDown}
    >
      <div
        ref={dialogRef}
        tabIndex={-1}
        className="bp-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={textId}
      >
        <h2 className="bp-dialog__title" id={titleId}>
          {title}
        </h2>
        <p className="bp-dialog__text" id={textId}>
          {children}
        </p>
        <div className="bp-dialog__actions">
          <Button ref={cancelRef} variant="ghost" type="button" disabled={busy} onClick={onCancel}>
            Cancel
          </Button>
          <Button ref={confirmRef} variant="danger-solid" type="button" disabled={busy} onClick={onConfirm}>
            {confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}
