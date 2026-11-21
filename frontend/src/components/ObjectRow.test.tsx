import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import ObjectRow from "./ObjectRow";
import type { BacnetObjectDTO } from "../api/types";

function makeObj(overrides: Partial<BacnetObjectDTO> = {}): BacnetObjectDTO {
  return {
    id: 5,
    device_id: 599002,
    object_type: "analog-input",
    instance_number: 1,
    name: "AI-1",
    description: "Simulated analog input",
    units: "degreesFahrenheit",
    bacnet_writable: true,
    out_of_service_default: false,
    relinquish_default: null,
    state_text: null,
    trended: false,
    alarming_enabled: false,
    ...overrides,
  } as BacnetObjectDTO;
}

function renderRow(obj: BacnetObjectDTO) {
  return render(
    <MemoryRouter>
      <table>
        <tbody>
          <ObjectRow
            obj={obj}
            deviceId={599002}
            selected={false}
            onToggleSelect={vi.fn()}
            onDelete={vi.fn()}
          />
        </tbody>
      </table>
    </MemoryRouter>
  );
}

describe("ObjectRow", () => {
  it("has no editable inputs except its selection checkbox", () => {
    const { container } = renderRow(makeObj());
    expect(container.querySelectorAll("select")).toHaveLength(0);
    const inputs = Array.from(container.querySelectorAll("input"));
    expect(inputs).toHaveLength(1);
    expect(inputs[0]).toHaveAttribute("type", "checkbox");
  });

  it("has no per-row Save button", () => {
    renderRow(makeObj());
    expect(screen.queryByRole("button", { name: /save/i })).not.toBeInTheDocument();
  });

  it("links to the object's overlay with a single Edit action", () => {
    renderRow(makeObj());
    expect(screen.getByRole("link", { name: /edit/i })).toHaveAttribute(
      "href",
      "/devices/599002/objects/5"
    );
  });

  it("opens the overlay as an in-app opener, so closing it goes Back", async () => {
    const user = userEvent.setup();
    let state: unknown = "unset";
    function Here() {
      state = useLocation().state;
      return null;
    }
    render(
      <MemoryRouter>
        <Here />
        <table>
          <tbody>
            <ObjectRow obj={makeObj()} deviceId={599002} selected={false} onToggleSelect={vi.fn()} onDelete={vi.fn()} />
          </tbody>
        </table>
      </MemoryRouter>
    );
    await user.click(screen.getByRole("link", { name: /edit/i }));
    expect(state).toEqual({ overlayFrom: "list" });
  });

  it("shows a flag badge only for the flags that are set", () => {
    renderRow(makeObj({ bacnet_writable: true, trended: true, alarming_enabled: false }));
    expect(screen.getByTitle("BACnet writable")).toBeInTheDocument();
    expect(screen.getByTitle("Trended")).toBeInTheDocument();
    expect(screen.queryByTitle("Alarming")).not.toBeInTheDocument();
  });

  it("shows the S flag on a simulated object", () => {
    renderRow(makeObj({ simulation: { kind: "noise", center: 1, amplitude: 1 } }));
    expect(screen.getByTitle("Simulated")).toBeInTheDocument();
  });

  it("shows the object's stored values as text", () => {
    renderRow(makeObj());
    expect(screen.getByText("analog-input:1")).toHaveClass("bp-cell-id");
    expect(screen.getByText("AI-1")).toBeInTheDocument();
    expect(screen.getByText("degreesFahrenheit")).toBeInTheDocument();
  });

  it("marks a selected row and shows an em dash for missing units", () => {
    render(
      <MemoryRouter>
        <table>
          <tbody>
            <ObjectRow obj={makeObj({ units: null })} deviceId={599002} selected onToggleSelect={vi.fn()} onDelete={vi.fn()} />
          </tbody>
        </table>
      </MemoryRouter>
    );
    // aria-selected is only valid on a grid's rows: a plain table marks it for the styles.
    expect(screen.getByRole("row")).toHaveAttribute("data-selected");
    expect(screen.getByRole("row")).not.toHaveAttribute("aria-selected");
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("clips the description with the whole text in its title", () => {
    renderRow(makeObj({ description: "A long description of this point" }));
    const cell = screen.getByText("A long description of this point");
    expect(cell).toHaveClass("bp-cell-clip");
    expect(cell).toHaveAttribute("title", "A long description of this point");
  });
});
