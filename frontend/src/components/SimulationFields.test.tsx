import { useState } from "react";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import SimulationFields from "./SimulationFields";
import { defaultSimulation, toValues, type SimFamily, type SimValues } from "../utils/simulation";
import { expectNamedControls } from "../testing/controls";

function Harness({
  family,
  kind,
  initial,
  ...rest
}: {
  family: SimFamily;
  kind: string;
  initial?: SimValues;
  unit?: string;
  errors?: Record<string, string>;
  disabled?: boolean;
  stateNames?: string[];
  sources?: { value: string; label: string; group: string }[];
}) {
  const [values, setValues] = useState(initial ?? toValues(defaultSimulation(family, kind)));
  return (
    <>
      <SimulationFields family={family} kind={kind} values={values} onChange={setValues} idPrefix="t" {...rest} />
      <output data-testid="values">{JSON.stringify(values)}</output>
    </>
  );
}

const stored = () => JSON.parse(screen.getByTestId("values").textContent ?? "{}");

describe("SimulationFields", () => {
  it("groups a kind's fields with the unit in the value labels", () => {
    render(<Harness family="analog" kind="sine" unit="°F" />);
    const signal = screen.getByRole("group", { name: "Signal" });
    expect(within(signal).getByLabelText("Center (°F)")).toHaveValue(50);
    expect(within(signal).getByLabelText("Amplitude ± (°F)")).toHaveValue(10);
    expect(within(screen.getByRole("group", { name: "Timing" })).getByLabelText("Phase offset (s)")).toBeInTheDocument();
    expect(within(screen.getByRole("group", { name: "Output" })).getByLabelText("Never below (°F)")).toHaveAttribute(
      "placeholder",
      "No limit"
    );
  });

  it("shows no Output group for a binary kind", () => {
    render(<Harness family="binary" kind="square" />);
    expect(screen.queryByRole("group", { name: "Output" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Duty (0–1)")).toBeInTheDocument();
  });

  it("shows a field's error under it", () => {
    render(<Harness family="analog" kind="triangle" errors={{ high: "High must be above low." }} />);
    const high = screen.getByLabelText("High");
    expect(high).toHaveAttribute("aria-invalid", "true");
    expect(high).toHaveAccessibleDescription("High must be above low.");
  });

  it("disables every field while simulation is off", () => {
    render(<Harness family="analog" kind="sine" disabled />);
    for (const group of screen.getAllByRole("group")) expect(group).toBeDisabled();
  });

  it("picks a Follow source from the device's analog objects", async () => {
    const user = userEvent.setup();
    render(
      <Harness family="analog" kind="follow" sources={[{ value: "analog-value:3", label: "analog-value:3 · Zone temp", group: "" }]} />
    );
    await user.type(screen.getByRole("combobox", { name: "Source object" }), "zone");
    await user.keyboard("{ArrowDown}{Enter}");
    expect(stored().source).toBe("analog-value:3");
  });

  it("chooses a multi-state kind's states from a checklist, keeping two", async () => {
    const user = userEvent.setup();
    render(<Harness family="multi-state" kind="cycle" stateNames={["Off", "Low", "High"]} />);
    const states = screen.getByRole("group", { name: "States" });
    const boxes = within(states).getAllByRole("checkbox");
    expect(boxes.map((b) => (b as HTMLInputElement).checked)).toEqual([true, true, true]);
    await user.click(within(states).getByRole("checkbox", { name: "2 · Low" }));
    expect(stored().states).toBe("1, 3");
    expect(within(states).getByRole("checkbox", { name: "1 · Off" })).toBeDisabled();
    await user.click(within(states).getByRole("checkbox", { name: "2 · Low" }));
    expect(stored().states).toBe("");
  });

  it("drops a stored state the object no longer has on the next change", async () => {
    const user = userEvent.setup();
    const initial = { ...toValues(defaultSimulation("multi-state", "cycle")), states: "1, 2, 3, 6" };
    render(<Harness family="multi-state" kind="cycle" initial={initial} stateNames={["Off", "Low", "High", "Auto"]} />);
    await user.click(within(screen.getByRole("group", { name: "States" })).getByRole("checkbox", { name: "3 · High" }));
    expect(stored().states).toBe("1, 2");
  });

  it("names every control", () => {
    const { container } = render(<Harness family="analog" kind="square" unit="%" />);
    expectNamedControls(container);
  });
});
