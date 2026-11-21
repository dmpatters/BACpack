import { useEffect } from "react";

// Most specific first: "Bugfix Session Device · Objects · BACpack".
export function usePageTitle(...parts: Array<string | undefined | null | false>): void {
  const title = [...parts.filter((p): p is string => Boolean(p)), "BACpack"].join(" · ");
  useEffect(() => {
    document.title = title;
  }, [title]);
}

// For a page that sits under a layer with a title of its own: the page's
// title waits until the layer is gone.
export function usePageTitleWhen(enabled: boolean, ...parts: Array<string | undefined | null | false>): void {
  const title = [...parts.filter((p): p is string => Boolean(p)), "BACpack"].join(" · ");
  useEffect(() => {
    if (enabled) document.title = title;
  }, [enabled, title]);
}
