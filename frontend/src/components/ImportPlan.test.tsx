import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import ImportPlan, { importPlanSummary, pastSummary } from "./ImportPlan";

describe("ImportPlan", () => {
  it("lists creates, updates and deletes with counts", () => {
    render(<ImportPlan groups={[{ title: "Device 599001", created: ["analog-value:3"], updated: [], deleted: ["analog-value:2"] }]} />);
    expect(screen.getByText(/Create 1:/)).toHaveTextContent("Create 1: analog-value:3");
    expect(screen.getByText(/Update 0/)).toHaveTextContent("Update 0: none");
    // Danger-coloured, in the list's own mono font (bp-field-error would set a sans font).
    expect(screen.getByText(/Delete 1:/)).toHaveStyle({ color: "var(--danger)" });
    expect(screen.getByText(/Delete 1:/)).not.toHaveClass("bp-field-error");
    expect(screen.queryByText("Device 599001")).not.toBeInTheDocument();
  });
  it("titles each group when there are several", () => {
    render(
      <ImportPlan
        groups={[
          { title: "Devices", created: [], updated: ["599001"], deleted: [] },
          { title: "Device 599001", created: [], updated: [], deleted: [] },
        ]}
      />
    );
    expect(screen.getByRole("heading", { name: "Device 599001" })).toBeInTheDocument();
  });
  it("keeps each id whole, so a line wraps between ids and never inside one", () => {
    const { container } = render(
      <ImportPlan groups={[{ title: "D", created: ["analog-value:1", "binary-input:22"], updated: [], deleted: [] }]} />
    );
    const ids = Array.from(container.querySelectorAll("li .bp-nowrap")).map((el) => el.textContent);
    expect(ids).toEqual(["analog-value:1", "binary-input:22"]);
    expect(screen.getByText(/Create 2:/)).toHaveTextContent("Create 2: analog-value:1, binary-input:22");
  });
  it("shortens a long list", () => {
    const many = Array.from({ length: 25 }, (_, i) => `analog-value:${i}`);
    render(<ImportPlan groups={[{ title: "D", created: many, updated: [], deleted: [] }]} />);
    expect(screen.getByText(/Create 25:/)).toHaveTextContent("and 5 more");
  });
  it("summarises in sentences with real plurals", () => {
    expect(importPlanSummary(1, 2, 0, "object")).toBe("Creates 1 object and updates 2 objects.");
    expect(importPlanSummary(0, 0, 0, "object")).toBe("No changes.");
    expect(importPlanSummary(0, 1, 3, "object")).toBe("Updates 1 object and deletes 3 objects.");
    expect(importPlanSummary(1, 1, 1, "device")).toBe("Creates 1 device, updates 1 device and deletes 1 device.");
    expect(pastSummary(0, 1, 1, "object")).toBe("Updated 1 object and deleted 1 object.");
    expect(pastSummary(0, 0, 0, "object")).toBe("No changes.");
  });
});
