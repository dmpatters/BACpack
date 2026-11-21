import { createContext, useContext, useEffect, useRef, type ReactNode } from "react";

type Layer = { handler: () => void };

// Escape closes the topmost open layer only. One document listener serves the
// whole stack; module-level so a piece rendered alone still works.
const stack: Layer[] = [];

function onDocumentKeyDown(e: KeyboardEvent) {
  if (e.key !== "Escape" || e.defaultPrevented) return;
  const top = stack[stack.length - 1];
  if (!top) return;
  e.preventDefault();
  top.handler();
}

export function useEscapeLayer(active: boolean, onEscape: () => void): void {
  const handler = useRef(onEscape);
  handler.current = onEscape;
  useEffect(() => {
    if (!active) return;
    const layer: Layer = { handler: () => handler.current() };
    if (stack.length === 0) document.addEventListener("keydown", onDocumentKeyDown);
    stack.push(layer);
    return () => {
      stack.splice(stack.indexOf(layer), 1);
      if (stack.length === 0) document.removeEventListener("keydown", onDocumentKeyDown);
    };
  }, [active]);
}

type Shell = { shell: () => HTMLElement[]; callers: { count: number } };
const ShellContext = createContext<Shell | null>(null);

export function LayersProvider({ children, shell }: { children: ReactNode; shell: () => HTMLElement[] }) {
  const value = useRef<Shell>({ shell, callers: { count: 0 } });
  value.current.shell = shell;
  return <ShellContext.Provider value={value.current}>{children}</ShellContext.Provider>;
}

// While an overlay is up, the nav and page behind it can't be reached by Tab
// or a screen reader.
export function useInertShell(active: boolean): void {
  const ctx = useContext(ShellContext);
  useEffect(() => {
    if (!active || !ctx) return;
    ctx.callers.count += 1;
    ctx.shell().forEach((el) => el.setAttribute("inert", ""));
    return () => {
      ctx.callers.count -= 1;
      if (ctx.callers.count === 0) ctx.shell().forEach((el) => el.removeAttribute("inert"));
    };
  }, [active, ctx]);
}

// A panel inside the overlay reserves room in the overlay's body, not in the
// page behind it.
export const PanelHostContext = createContext<{ current: HTMLElement | null } | null>(null);

export function usePanelOpen(open: boolean): void {
  const host = useContext(PanelHostContext);
  useEffect(() => {
    if (!open) return;
    const el = host ? host.current : document.querySelector<HTMLElement>("main");
    el?.setAttribute("data-panel-open", "");
    return () => el?.removeAttribute("data-panel-open");
  }, [open, host]);
}

// Focus goes into a layer when it opens and back to the control that opened it
// (or the page's main, if that control is gone, or there was none: a layer
// opened by a link starts with focus on the body) when it closes.
export function focusOpener(opener: HTMLElement | null): void {
  if (opener && opener !== document.body && opener.isConnected) opener.focus();
  else document.querySelector<HTMLElement>("main")?.focus();
}
