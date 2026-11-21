// Which column of a `bp-columns` or `bp-tab-split` layout holds `el`: 0 for the
// first, 1 for the second, null when it isn't in one.
export function columnOf(el: Element): number | null {
  const column = el.closest(".bp-columns > *, .bp-tab-split > *");
  if (!column?.parentElement) return null;
  return Array.from(column.parentElement.children).indexOf(column);
}

// Whether `el` stands in a settings page's single centred column.
export function inSettingsColumn(el: Element): boolean {
  return el.closest(".bp-settings") !== null;
}
