import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import BulkSimulatePanel from "./BulkSimulatePanel";

describe("BulkSimulatePanel", () => {
  it("offers the shared family's kinds and sends random phase by default", async () => {
    const onApply = vi.fn();
    const user = userEvent.setup();
    render(
      <BulkSimulatePanel
        objectTypes={["analog-input", "analog-value"]}
        selectedCount={2}
        applying={false}
        onApply={onApply}
        onCancel={() => {}}
      />
    );
    const options = [...(screen.getByLabelText("Kind") as HTMLSelectElement).options].map((o) => o.value);
    expect(options).toEqual(["sine", "triangle", "sawtooth", "square", "random_walk", "noise", "follow"]);
    await user.click(screen.getByRole("button", { name: "Simulate 2 objects" }));
    expect(onApply).toHaveBeenCalledWith(
      { kind: "sine", center: 50, amplitude: 10, period_s: 600, interval_s: 5, decimals: 2 },
      true
    );
  });

  it("sends no random phase for a kind without a cycle", async () => {
    const onApply = vi.fn();
    const user = userEvent.setup();
    render(
      <BulkSimulatePanel objectTypes={["binary-value"]} selectedCount={1} applying={false} onApply={onApply} onCancel={() => {}} />
    );
    await user.selectOptions(screen.getByLabelText("Kind"), "random");
    expect(screen.queryByLabelText(/Random phase/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Simulate 1 object" }));
    expect(onApply).toHaveBeenCalledWith({ kind: "random", min_dwell_s: 30, max_dwell_s: 120 }, false);
  });

  it("explains a selection with no family in common", () => {
    render(
      <BulkSimulatePanel
        objectTypes={["analog-value", "binary-value"]}
        selectedCount={2}
        applying={false}
        onApply={() => {}}
        onCancel={() => {}}
      />
    );
    expect(screen.getByText(/no simulator in common/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Kind")).not.toBeInTheDocument();
  });
});
