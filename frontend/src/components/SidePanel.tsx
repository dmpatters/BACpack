import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import Button from "./Button";
import { focusOpener, useEscapeLayer, usePanelOpen } from "./layers";
import { useTabShown } from "./Tabs";

// A form that sits beside a list instead of replacing it. It mounts on the
// first open; closing hides it rather than unmounting, so a half-typed form
// survives. A click elsewhere does not close it: the page beside it is meant
// to be used.
//
// `focusKey` moves focus into the panel again when it changes while the panel
// stays open (a different row's form in the same panel).
export default function SidePanel({
  open,
  title,
  onClose,
  focusKey,
  children,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  focusKey?: string | number;
  children: ReactNode;
}) {
  const [opened, setOpened] = useState(open);
  const titleId = useId();
  const panel = useRef<HTMLElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  const wasActive = useRef(false);
  const wasOpen = useRef(false);
  // A panel in a tab that isn't shown is out of the way: it holds neither
  // the Escape key nor the page's reserved space, and comes back with the tab.
  const tabShown = useTabShown();
  const active = open && tabShown;

  if (open && !opened) setOpened(true);

  useEscapeLayer(active, onClose);
  usePanelOpen(active);

  const focusFirst = () => {
    const first = panel.current?.querySelector<HTMLElement>("input, select, textarea");
    (first ?? panel.current?.querySelector<HTMLElement>("button"))?.focus();
  };

  // Coming back to the panel's tab is not an open: the opener and the focus
  // stay where they are, and only a real close returns focus.
  useEffect(() => {
    if (active && !wasActive.current) {
      if (!wasOpen.current) {
        opener.current = document.activeElement as HTMLElement | null;
        focusFirst();
      }
      wasActive.current = true;
    } else if (!active && wasActive.current) {
      wasActive.current = false;
      // Another panel that opened in the same commit has focus already.
      const now = document.activeElement;
      if (!open && (!now || now === document.body || panel.current?.contains(now))) focusOpener(opener.current);
    }
    wasOpen.current = open;
  }, [active, open, opened]);

  const firstKey = useRef(focusKey);
  useEffect(() => {
    if (focusKey === firstKey.current) return;
    firstKey.current = focusKey;
    if (active) focusFirst();
  }, [focusKey]);

  if (!opened) return null;
  return (
    <section ref={panel} className="bp-panel" role="dialog" aria-modal="false" aria-labelledby={titleId} hidden={!open}>
      <header className="bp-panel__head">
        <h2 className="bp-panel__title" id={titleId}>
          {title}
        </h2>
        <Button type="button" variant="ghost" small aria-label="Close" onClick={onClose}>
          ×
        </Button>
      </header>
      <div className="bp-panel__body">{children}</div>
    </section>
  );
}
