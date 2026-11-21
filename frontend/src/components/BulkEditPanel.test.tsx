import type { ReactElement } from "react";
import { render as rtlRender, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import BulkEditPanel from "./BulkEditPanel";
import { expectNamedControls } from "../testing/controls";

vi.mock("../api/endpoints", () => ({
  unitsApi: { list: vi.fn().mockResolvedValue({ units: ["degreesFahrenheit", "percent"] }) },
}));

const render = (ui: ReactElement) =>
  rtlRender(<QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>);

describe("BulkEditPanel", () => {
  it("applies only the fields the user checked", async () => {
    const user = userEvent.setup();
    const onApply = vi.fn();
    render(<BulkEditPanel selectedCount={3} onApply={onApply} onCancel={() => {}} applying={false} />);

    await user.click(screen.getByRole("checkbox", { name: "Set BACnet writable" }));
    // The value defaults to true: uncheck it.
    await user.click(screen.getByRole("checkbox", { name: "BACnet writable" }));

    await user.click(screen.getByRole("button", { name: "Apply to 3 objects" }));

    expect(onApply).toHaveBeenCalledWith({ bacnet_writable: false });
  });

  it("disables the value input until its field checkbox is checked", async () => {
    const user = userEvent.setup();
    render(<BulkEditPanel selectedCount={1} onApply={() => {}} onCancel={() => {}} applying={false} />);
    const description = screen.getByRole("textbox", { name: "Description" });
    expect(description).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: "Set description" }));
    expect(description).toBeEnabled();
    expect(screen.getByRole("button", { name: "Apply to 1 object" })).toBeInTheDocument();
  });

  it("says BACnet writable covers only Present_Value on points", () => {
    render(<BulkEditPanel selectedCount={2} onApply={() => {}} onCancel={() => {}} applying={false} />);
    expect(screen.getByRole("checkbox", { name: "BACnet writable" })).toHaveAccessibleDescription(
      "Points: Present_Value only. Schedules and calendars: all configuration."
    );
  });

  it("calls onCancel when Cancel is clicked", async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    render(<BulkEditPanel selectedCount={2} onApply={() => {}} onCancel={onCancel} applying={false} />);
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalled();
  });

  it("sets units from the same picker as single edit", async () => {
    const user = userEvent.setup();
    const onApply = vi.fn();
    render(<BulkEditPanel selectedCount={3} onApply={onApply} onCancel={() => {}} applying={false} />);
    await user.click(screen.getByRole("checkbox", { name: "Set units" }));
    const units = await screen.findByRole("combobox", { name: "Units" });
    await user.type(units, "fahr");
    await user.keyboard("{Enter}");
    await user.click(screen.getByRole("button", { name: "Apply to 3 objects" }));
    expect(onApply).toHaveBeenCalledWith(expect.objectContaining({ units: "degreesFahrenheit" }));
  });

  it("shows a yes/no value as a labelled checkbox", () => {
    render(<BulkEditPanel selectedCount={2} onApply={() => {}} onCancel={() => {}} applying={false} />);
    for (const name of ["BACnet writable", "Out of service by default", "Alarming enabled", "Trended"]) {
      expect(screen.getByRole("checkbox", { name }).closest("label")).toHaveClass("bp-check");
    }
  });

  it("names every value control after its field", () => {
    const { container } = render(<BulkEditPanel selectedCount={2} onApply={() => {}} onCancel={() => {}} applying={false} />);
    expectNamedControls(container);
  });
});

describe("BulkEditPanel with a notification class selected", () => {
  const types = ["analog-value", "notification-class"];

  it("explains and blocks Trended on", async () => {
    const user = userEvent.setup();
    render(<BulkEditPanel selectedCount={2} selectedTypes={types} onApply={() => {}} onCancel={() => {}} applying={false} />);
    await user.click(screen.getByRole("checkbox", { name: "Set trended" }));
    await user.click(screen.getByRole("checkbox", { name: "Trended" }));
    expect(screen.getByRole("checkbox", { name: "Trended" })).toHaveAccessibleDescription(
      "A Notification Class in the selection can't be trended. Deselect it."
    );
    expect(screen.getByRole("button", { name: "Apply to 2 objects" })).toBeDisabled();
  });

  it("lets Trended be switched off", async () => {
    const user = userEvent.setup();
    const onApply = vi.fn();
    render(<BulkEditPanel selectedCount={2} selectedTypes={types} onApply={onApply} onCancel={() => {}} applying={false} />);
    await user.click(screen.getByRole("checkbox", { name: "Set trended" }));
    expect(screen.getByRole("button", { name: "Apply to 2 objects" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Apply to 2 objects" }));
    expect(onApply).toHaveBeenCalledWith({ trended: false, trend_reporting_enabled: false });
  });

  it("is unchanged without selectedTypes", async () => {
    const user = userEvent.setup();
    render(<BulkEditPanel selectedCount={2} onApply={() => {}} onCancel={() => {}} applying={false} />);
    await user.click(screen.getByRole("checkbox", { name: "Set trended" }));
    await user.click(screen.getByRole("checkbox", { name: "Trended" }));
    expect(screen.getByRole("button", { name: "Apply to 2 objects" })).toBeEnabled();
  });
});

describe("BulkEditPanel with nothing selected", () => {
  it("reads 'Apply to 0 objects' and cannot be applied", () => {
    render(<BulkEditPanel selectedCount={0} onApply={() => {}} onCancel={() => {}} applying={false} />);
    expect(screen.getByRole("button", { name: "Apply to 0 objects" })).toBeDisabled();
  });

  it("reads 'Apply to 1 object' for one", () => {
    render(<BulkEditPanel selectedCount={1} onApply={() => {}} onCancel={() => {}} applying={false} />);
    expect(screen.getByRole("button", { name: "Apply to 1 object" })).toBeEnabled();
  });
});
