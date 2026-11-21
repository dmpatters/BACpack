import { useId, useRef, type KeyboardEvent, type RefObject } from "react";
import { createPortal } from "react-dom";
import { LayersProvider, useEscapeLayer, useInertShell } from "./layers";
import LoginForm from "./LoginForm";

function Dialog({ onSignedIn, scrim }: { onSignedIn: () => void; scrim: RefObject<HTMLDivElement> }) {
  const titleId = useId();
  // A layer of its own, so Escape never reaches a panel or overlay under it.
  useEscapeLayer(true, () => {});
  useInertShell(true);
  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key === "Escape") e.preventDefault();
  };
  return (
    <div ref={scrim} className="bp-scrim" onKeyDown={onKeyDown}>
      <div className="bp-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <h2 className="bp-dialog__title" id={titleId}>
          Your session has ended
        </h2>
        <p className="bp-dialog__text">Sign in again to continue.</p>
        <LoginForm onSignedIn={onSignedIn} />
      </div>
    </div>
  );
}

// Shown over the page when a call finds the session gone. The page stays
// mounted behind it, so an open form keeps what was typed. It can't be
// dismissed: nothing works until you sign in again. It renders under the body
// like EditOverlay and makes every other child of the body inert: AuthGate
// sits outside Layout (so there is no shell provider above it), and an open
// overlay or panel is a body child outside the app root.
export default function SessionExpiredDialog({ onSignedIn }: { onSignedIn: () => void }) {
  const scrim = useRef<HTMLDivElement>(null);
  return createPortal(
    <LayersProvider
      shell={() => Array.from(document.body.children).filter((el): el is HTMLElement => el !== scrim.current && el instanceof HTMLElement)}
    >
      <Dialog onSignedIn={onSignedIn} scrim={scrim} />
    </LayersProvider>,
    document.body
  );
}
