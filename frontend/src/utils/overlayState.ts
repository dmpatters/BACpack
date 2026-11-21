// Location state an in-app opener pushes with an object's URL: the list is
// behind the overlay, so closing it is Back rather than a new entry.
export const OVERLAY_FROM_LIST = { overlayFrom: "list" } as const;

export function openedFromList(state: unknown): boolean {
  return (state as { overlayFrom?: string } | null)?.overlayFrom === OVERLAY_FROM_LIST.overlayFrom;
}
