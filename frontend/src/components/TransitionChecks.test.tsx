import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ALL_TRANSITIONS, TransitionChecks, transitionsOrDefault } from "./TransitionChecks";

function renderRow(value: [boolean, boolean, boolean], onChange = vi.fn()) {
  render(
    <TransitionChecks legend="Event enable" help="Which transitions send a notification." value={value} onChange={onChange} idPrefix="ee" />
  );
  return onChange;
}

describe("TransitionChecks", () => {
  it("shows the three transitions in BACnet order", () => {
    renderRow([true, true, true]);
    const labels = screen.getAllByRole("checkbox").map((box) => box.closest("label")?.textContent);
    expect(labels).toEqual(["To off-normal", "To fault", "To normal"]);
  });

  it("reports the flipped slot only", async () => {
    const user = userEvent.setup();
    const onChange = renderRow([true, true, true]);
    await user.click(screen.getByLabelText("To fault"));
    expect(onChange).toHaveBeenCalledWith([true, false, true]);
  });

  it("defaults null to all transitions", () => {
    expect(transitionsOrDefault(null)).toEqual([true, true, true]);
    expect(transitionsOrDefault(undefined)).toEqual(ALL_TRANSITIONS);
    const given: [boolean, boolean, boolean] = [false, true, false];
    expect(transitionsOrDefault(given)).toBe(given);
  });

  it("names its checkboxes inside the group", () => {
    renderRow([true, false, true]);
    const group = screen.getByRole("group", { name: "Event enable" });
    expect(within(group).getAllByRole("checkbox")).toHaveLength(3);
    expect(within(group).getByLabelText("To normal")).toBeChecked();
    expect(within(group).getByLabelText("To fault")).not.toBeChecked();
    expect(within(group).getByText("Which transitions send a notification.")).toBeInTheDocument();
  });
});
