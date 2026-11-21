import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import Button from "./Button";
import { focusOpener, PanelHostContext, useEscapeLayer, useInertShell } from "./layers";

// The whole object page in a near-full-screen layer over the list. A click on
// the scrim does not close it: leaving with unsaved input is the router
// guard's call, not a stray click's.
export default function EditOverlay({
  title,
  crumb,
  onClose,
  children,
}: {
  title: string;
  // Plain text on the left of the top bar: where this layer is.
  crumb?: ReactNode;
  onClose: () => void;
  children: ReactNode;
}) {
  const frame = useRef<HTMLDivElement>(null);
  const body = useRef<HTMLDivElement>(null);

  // Taken while rendering: making the page inert can blur what had focus.
  const [opener] = useState(() => document.activeElement as HTMLElement | null);

  useEscapeLayer(true, onClose);
  useInertShell(true);

  useEffect(() => {
    document.documentElement.classList.add("bp-noscroll");
    frame.current?.querySelector<HTMLElement>(".bp-overlay__close")?.focus();
    return () => {
      document.documentElement.classList.remove("bp-noscroll");
      focusOpener(opener);
    };
  }, []);

  return createPortal(
    <div className="bp-overlay">
      <div ref={frame} className="bp-overlay__frame" role="dialog" aria-modal="true" aria-label={title}>
        <div className="bp-overlay__bar">
          {crumb !== undefined && <span className="bp-overlay__crumb">{crumb}</span>}
          <Button type="button" variant="ghost" small className="bp-overlay__close" aria-label="Close" onClick={onClose}>
            ×
          </Button>
        </div>
        <div ref={body} className="bp-overlay__body">
          <PanelHostContext.Provider value={body}>{children}</PanelHostContext.Provider>
        </div>
      </div>
    </div>,
    document.body
  );
}
