import { within } from "@testing-library/react";
import { expect } from "vitest";

const CONTROL_ROLES = ["textbox", "searchbox", "spinbutton", "combobox", "checkbox", "radio", "listbox", "slider"] as const;

// "Every control labelled" (docs/design-system/components/Form): a control
// with no name, or the "on" a checkbox gets from its value, fails.
export function expectNamedControls(root: HTMLElement): void {
  const controls = [
    ...CONTROL_ROLES.flatMap((role) => within(root).queryAllByRole(role)),
    ...Array.from(root.querySelectorAll<HTMLElement>('input[type="file"], input[type="time"], input[type="date"]')),
  ];
  for (const control of controls) {
    const what = control.outerHTML.slice(0, 120);
    expect(control, what).toHaveAccessibleName(/\S/);
    expect(control, what).not.toHaveAccessibleName("on");
  }
}
