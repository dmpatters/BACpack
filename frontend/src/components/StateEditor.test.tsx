import { useState } from "react";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import StateEditor from "./StateEditor";
import type { StateEdit, StateReference } from "../utils/stateNames";
import { expectNamedControls } from "../testing/controls";

function Harness({
  initial,
  savedNames,
  references = [],
  onEdit,
}: {
  initial: string[];
  savedNames?: string[];
  references?: StateReference[];
  onEdit?: (edit: StateEdit) => void;
}) {
  const [names, setNames] = useState(initial);
  return (
    <StateEditor
      names={names}
      savedNames={savedNames ?? initial}
      references={references}
      onChange={(edit) => {
        onEdit?.(edit);
        setNames(edit.names);
      }}
    />
  );
}

const values = () => screen.getAllByRole("textbox").map((input) => (input as HTMLInputElement).value);

describe("StateEditor", () => {
  it("numbers the states and says the count in words", () => {
    const { rerender } = render(<Harness initial={["Off", "Low", "High"]} />);
    expect(screen.getByLabelText("Name of state 2")).toHaveValue("Low");
    expect(screen.getByText("3 states")).toBeInTheDocument();
    rerender(<Harness key="one" initial={["Off"]} />);
    expect(screen.getByText("1 state")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete state 1" })).toBeDisabled();
  });

  it("moves a state and keeps focus with it", async () => {
    const user = userEvent.setup();
    const onEdit = vi.fn();
    render(<Harness initial={["Off", "Low", "High"]} onEdit={onEdit} />);
    await user.click(screen.getByRole("button", { name: "Move state 1 down" }));
    expect(values()).toEqual(["Low", "Off", "High"]);
    expect(onEdit).toHaveBeenLastCalledWith({ names: ["Low", "Off", "High"], renumber: [2, 1, 3] });
    expect(screen.getByRole("button", { name: "Move state 2 down" })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(values()).toEqual(["Low", "High", "Off"]);
    // At the bottom its Move down is disabled, so focus goes to its Move up.
    expect(screen.getByRole("button", { name: "Move state 3 up" })).toHaveFocus();
  });

  it("flags a blank or repeated name next to its row", () => {
    render(<Harness initial={["Off", "", "Off"]} />);
    const blank = screen.getByLabelText("Name of state 2");
    expect(blank).toHaveAttribute("aria-invalid", "true");
    expect(blank).toHaveAccessibleDescription("State 2 needs a name.");
    expect(screen.getByLabelText("Name of state 3")).toHaveAccessibleDescription(
      "State 3 repeats the name of state 1."
    );
    expect(screen.getByLabelText("Name of state 1")).not.toHaveAttribute("aria-invalid");
    expect(screen.getByLabelText("Name of state 1").closest("li")).not.toHaveClass("bp-state--error");
    expect(blank.closest("li")).toHaveClass("bp-state--error");
  });

  it("deletes an unused state at once and focuses the next name", async () => {
    const user = userEvent.setup();
    render(<Harness initial={["Off", "Low", "High"]} />);
    await user.click(screen.getByRole("button", { name: "Delete state 2" }));
    expect(values()).toEqual(["Off", "High"]);
    expect(screen.getByLabelText("Name of state 2")).toHaveFocus();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("asks before deleting a state another object uses", async () => {
    const user = userEvent.setup();
    render(<Harness initial={["Off", "Low", "High"]} references={[{ who: "Schedule OCC", states: [2] }]} />);
    await user.click(screen.getByRole("button", { name: "Delete state 2" }));
    const dialog = screen.getByRole("alertdialog", { name: "Delete state 2 (Low)?" });
    expect(dialog).toHaveTextContent("Schedule OCC uses state 2 (Low).");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(values()).toEqual(["Off", "Low", "High"]);
    await user.click(screen.getByRole("button", { name: "Delete state 2" }));
    await user.click(screen.getByRole("button", { name: "Delete state" }));
    expect(values()).toEqual(["Off", "High"]);
  });

  it("asks about the saved state a row was, after a move", async () => {
    const user = userEvent.setup();
    render(<Harness initial={["Off", "Low", "High"]} references={[{ who: "Schedule OCC", states: [2] }]} />);
    await user.click(screen.getByRole("button", { name: "Move state 2 up" }));
    await user.click(screen.getByRole("button", { name: "Delete state 2" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(values()).toEqual(["Low", "High"]);
    await user.click(screen.getByRole("button", { name: "Delete state 1" }));
    expect(screen.getByRole("alertdialog", { name: "Delete state 1 (Low)?" })).toBeInTheDocument();
  });

  it("still knows a moved row's saved state after a refetch hands over an equal list", async () => {
    const user = userEvent.setup();
    const refs = [{ who: "Schedule OCC", states: [3] }];
    const { rerender } = render(<Harness initial={["Off", "Low", "High"]} savedNames={["Off", "Low", "High"]} references={refs} />);
    await user.click(screen.getByRole("button", { name: "Move state 3 up" }));
    rerender(<Harness initial={["Off", "Low", "High"]} savedNames={["Off", "Low", "High"]} references={refs} />);
    await user.click(screen.getByRole("button", { name: "Delete state 2" }));
    expect(screen.getByRole("alertdialog", { name: "Delete state 2 (High)?" })).toBeInTheDocument();
  });

  it("asks before a preset drops a state another object uses", async () => {
    const user = userEvent.setup();
    render(
      <Harness initial={["Off", "Low", "Med", "High"]} references={[{ who: "Schedule OCC", states: [4] }]} />
    );
    await user.click(screen.getByRole("button", { name: "Off / On" }));
    const dialog = screen.getByRole("alertdialog", { name: "Replace the states with Off / On?" });
    expect(dialog).toHaveTextContent("Schedule OCC uses state 4 (High).");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(values()).toEqual(["Off", "Low", "Med", "High"]);
    await user.click(screen.getByRole("button", { name: "Off / On" }));
    await user.click(screen.getByRole("button", { name: "Replace states" }));
    expect(values()).toEqual(["Off", "On"]);
  });

  it("fills a preset at once when the states it drops are unused", async () => {
    const user = userEvent.setup();
    render(
      <Harness initial={["Off", "Low", "Med", "High"]} references={[{ who: "Schedule OCC", states: [2] }]} />
    );
    await user.click(screen.getByRole("button", { name: "Off / On" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(values()).toEqual(["Off", "On"]);
  });

  it("warns that reordering renumbers the states others use", () => {
    render(<Harness initial={["Off", "Low", "Medium"]} references={[{ who: "Schedule OCC_SCHEDULE", states: [3] }]} />);
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Reordering renumbers states. Schedule OCC_SCHEDULE uses state 3 (Medium). Update it after moving or deleting states."
    );
  });

  it("adds a state and fills from a preset", async () => {
    const user = userEvent.setup();
    render(<Harness initial={["Off"]} />);
    await user.click(screen.getByRole("button", { name: "Add state" }));
    expect(values()).toEqual(["Off", "State 2"]);
    expect(screen.getByLabelText("Name of state 2")).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Occupied / Standby / Unoccupied" }));
    expect(values()).toEqual(["Occupied", "Standby", "Unoccupied"]);
  });

  it("stops adding at 254 states", () => {
    render(<Harness initial={Array.from({ length: 254 }, (_, i) => `S${i + 1}`)} />);
    expect(screen.getByRole("button", { name: "Add state" })).toBeDisabled();
  });

  it("names every control", () => {
    const { container } = render(<Harness initial={["Off", "On"]} />);
    expectNamedControls(container);
  });
});
