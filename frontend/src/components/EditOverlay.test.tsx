import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import EditOverlay from "./EditOverlay";
import { LayersProvider } from "./layers";
import SidePanel from "./SidePanel";

describe("EditOverlay", () => {
  it("renders into document.body as a modal dialog and closes with the x and Escape", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const { container } = render(
      <EditOverlay title="Edit AV 1" onClose={onClose}>
        <p>the object page</p>
      </EditOverlay>
    );
    const dialog = screen.getByRole("dialog", { name: "Edit AV 1" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(container).not.toContainElement(dialog);
    expect(document.body).toContainElement(dialog);
    expect(document.documentElement).toHaveClass("bp-noscroll");

    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("does not close on a click on the scrim", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(
      <EditOverlay title="Edit AV 1" onClose={onClose}>
        <p>x</p>
      </EditOverlay>
    );
    await user.click(document.querySelector(".bp-overlay")!);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("makes the shell inert while it is up and drops the scroll lock on unmount", () => {
    function Page({ overlay }: { overlay: boolean }) {
      return (
        <LayersProvider shell={() => [document.getElementById("nav")!, document.getElementById("page")!]}>
          <nav id="nav" />
          <main id="page" />
          {overlay && (
            <EditOverlay title="Edit AV 1" onClose={() => {}}>
              <p>x</p>
            </EditOverlay>
          )}
        </LayersProvider>
      );
    }
    const { rerender } = render(<Page overlay />);
    expect(document.getElementById("nav")).toHaveAttribute("inert");
    expect(document.getElementById("page")).toHaveAttribute("inert");
    rerender(<Page overlay={false} />);
    expect(document.getElementById("nav")).not.toHaveAttribute("inert");
    expect(document.getElementById("page")).not.toHaveAttribute("inert");
    expect(document.documentElement).not.toHaveClass("bp-noscroll");
  });

  it("Escape closes a panel inside it first, then the overlay", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    function Harness() {
      const [panel, setPanel] = useState(false);
      return (
        <EditOverlay title="Edit calendar" onClose={onClose}>
          <button onClick={() => setPanel(true)}>Add entry</button>
          <SidePanel open={panel} title="Add entry" onClose={() => setPanel(false)}>
            <input aria-label="Date" />
          </SidePanel>
        </EditOverlay>
      );
    }
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Add entry" }));
    expect(screen.getByRole("dialog", { name: "Add entry" })).toBeVisible();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Add entry" })).not.toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("marks its body when a panel inside it is open", () => {
    render(
      <EditOverlay title="Edit calendar" onClose={() => {}}>
        <SidePanel open title="Add entry" onClose={() => {}}>
          <input aria-label="Date" />
        </SidePanel>
      </EditOverlay>
    );
    expect(document.querySelector(".bp-overlay__body")).toHaveAttribute("data-panel-open");
  });
});
