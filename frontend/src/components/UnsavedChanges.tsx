import { createContext, useContext, useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { useBlocker } from "react-router-dom";
import ConfirmDialog from "./ConfirmDialog";

type Registry = { set: (key: string, page: string | null) => void; pages: () => string[] };

const UnsavedContext = createContext<Registry | null>(null);

export const LEAVE_UNSAVED = { leaveUnsaved: true } as const;

export function differs(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) !== JSON.stringify(b);
}

// A refetch (another tab's save, a live update) refreshes the fields the user
// hasn't touched since the baseline and keeps the ones they have.
export function rebase<T extends object>(form: T, baseline: T, fresh: T): T {
  const out = { ...fresh };
  for (const key of Object.keys(form) as (keyof T)[]) {
    if (differs(form[key], baseline[key])) out[key] = form[key];
  }
  return out;
}

// An edit form's draft and the baseline it's dirty against: what the server
// last confirmed, i.e. the loaded source or the values a save just sent (so the
// form is clean before the refetch lands). A new source rebases the draft.
export function useDraft<S, T extends object>(source: S, load: (source: S) => T) {
  const [form, setForm] = useState(() => load(source));
  const [baseline, setBaseline] = useState(form);
  const baselineRef = useRef(baseline);
  baselineRef.current = baseline;
  const loadedFrom = useRef(source);
  useEffect(() => {
    if (loadedFrom.current === source) return;
    loadedFrom.current = source;
    const fresh = load(source);
    const base = baselineRef.current;
    setForm((f) => rebase(f, base, fresh));
    setBaseline(fresh);
    // load is usually a fresh closure each render; only a new source reloads.
  }, [source]);
  return { form, setForm, baseline, setBaseline };
}

// A page's tabs keep their drafts mounted (TabPanels), so only a path that
// isn't the page or one of its tabs drops an edit. A device page also stays
// mounted under an object's overlay.
export function staysOnPage(page: string, pathname: string): boolean {
  if (pathname === page) return true;
  if (!pathname.startsWith(`${page}/`)) return false;
  const rest = pathname.slice(page.length + 1);
  if (/^\/devices\/\d+$/.test(page) && rest.startsWith("objects/")) return true;
  return !rest.includes("/");
}

export function UnsavedChangesProvider({ children }: { children: ReactNode }) {
  // A ref, not state: forms report on every keystroke and nothing re-renders
  // from it; the guard reads it only when a navigation or unload happens.
  const dirty = useRef(new Map<string, string>());
  const registry = useMemo<Registry>(
    () => ({
      set: (key, page) => {
        if (page === null) dirty.current.delete(key);
        else dirty.current.set(key, page);
      },
      pages: () => [...dirty.current.values()],
    }),
    []
  );
  return <UnsavedContext.Provider value={registry}>{children}</UnsavedContext.Provider>;
}

export function useUnsavedChanges(dirty: boolean, page: string): void {
  const registry = useContext(UnsavedContext);
  const key = useId();
  useEffect(() => {
    if (!registry) return;
    registry.set(key, dirty ? page : null);
    return () => registry.set(key, null);
  }, [registry, key, dirty, page]);
}

export function LeaveGuard() {
  const registry = useContext(UnsavedContext);
  const blocker = useBlocker(({ nextLocation }) => {
    if (!registry) return false;
    if ((nextLocation.state as { leaveUnsaved?: boolean } | null)?.leaveUnsaved) return false;
    return registry.pages().some((page) => !staysOnPage(page, nextLocation.pathname));
  });

  useEffect(() => {
    if (!registry) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      if (registry.pages().length === 0) return;
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [registry]);

  if (blocker.state !== "blocked") return null;
  return (
    <ConfirmDialog
      title="Discard unsaved changes?"
      confirmLabel="Discard changes"
      onCancel={() => blocker.reset()}
      onConfirm={() => blocker.proceed()}
    >
      Leaving drops your unsaved edits.
    </ConfirmDialog>
  );
}
