import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import Combobox from "./Combobox";
import SidePanel from "./SidePanel";
import { TabPanels } from "./Tabs";

function Harness({ onClose = () => {} }: { onClose?: () => void }) {
  const [open, setOpen] = useState(false);
  const [showOpener, setShowOpener] = useState(true);
  return (
    <main tabIndex={-1} data-testid="main">
      {showOpener && <button onClick={() => setOpen(true)}>Open it</button>}
      <button onClick={() => setShowOpener(false)}>Remove opener</button>
      <SidePanel
        open={open}
        title="Add thing"
        onClose={() => {
          onClose();
          setOpen(false);
        }}
      >
        <input aria-label="Name" />
        <Combobox
          id="units"
          value=""
          options={[{ value: "a", label: "alpha", group: "g" }]}
          onChange={() => {}}
          emptyText={() => "none"}
        />
      </SidePanel>
    </main>
  );
}

describe("SidePanel", () => {
  it("renders nothing until first opened", () => {
    render(<Harness />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("opens with the first field focused", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByText("Open it"));
    expect(screen.getByRole("dialog", { name: "Add thing" })).toBeVisible();
    expect(screen.getByLabelText("Name")).toHaveFocus();
  });

  it("keeps the typed text across a close with the x button and with Escape, and returns focus to the opener", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByText("Open it"));
    await user.type(screen.getByLabelText("Name"), "pump");

    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog", { name: "Add thing" })).not.toBeInTheDocument();
    expect(screen.getByText("Open it")).toHaveFocus();

    await user.click(screen.getByText("Open it"));
    expect(screen.getByLabelText("Name")).toHaveValue("pump");

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Add thing" })).not.toBeInTheDocument();
    expect(screen.getByText("Open it")).toHaveFocus();

    await user.click(screen.getByText("Open it"));
    expect(screen.getByLabelText("Name")).toHaveValue("pump");
  });

  it("does not close on a click elsewhere", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<Harness onClose={onClose} />);
    await user.click(screen.getByText("Open it"));
    await user.click(document.body);
    await user.click(screen.getByTestId("main"));
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Add thing" })).toBeVisible();
  });

  it("an open Combobox takes the Escape; the panel stays", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<Harness onClose={onClose} />);
    await user.click(screen.getByText("Open it"));
    const units = screen.getByRole("combobox");
    await user.click(units);
    expect(units).toHaveAttribute("aria-expanded", "true");
    await user.keyboard("{Escape}");
    expect(units).toHaveAttribute("aria-expanded", "false");
    expect(onClose).not.toHaveBeenCalled();
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("moves focus into the panel when its focusKey changes while it stays open", async () => {
    function Keyed() {
      const [row, setRow] = useState<string | null>(null);
      return (
        <main>
          <button onClick={() => setRow("a")}>Edit a</button>
          <button onClick={() => setRow("b")}>Edit b</button>
          <SidePanel open={row !== null} title="Edit row" focusKey={row ?? undefined} onClose={() => setRow(null)}>
            <input aria-label="Name" />
          </SidePanel>
        </main>
      );
    }
    const user = userEvent.setup();
    render(<Keyed />);
    await user.click(screen.getByText("Edit a"));
    await user.click(screen.getByText("Edit b"));
    expect(screen.getByLabelText("Name")).toHaveFocus();
  });

  it("puts focus on main when the opener is gone", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByText("Open it"));
    await user.click(screen.getByText("Remove opener"));
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.getByTestId("main")).toHaveFocus();
  });
});

describe("SidePanel in a tab that is not shown", () => {
  function Tabbed({ onClose }: { onClose: () => void }) {
    const [tab, setTab] = useState("one");
    return (
      <main tabIndex={-1} data-testid="main">
        <button onClick={() => setTab("two")}>to two</button>
        <button onClick={() => setTab("one")}>to one</button>
        <TabPanels
          active={tab}
          panels={[
            { key: "one", render: () => <SidePanel open title="Add thing" onClose={onClose}><input aria-label="Name" /></SidePanel> },
            { key: "two", render: () => <p>second tab</p> },
          ]}
        />
      </main>
    );
  }

  it("gives up the reserved space and the Escape key, and takes them back with the tab", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<Tabbed onClose={onClose} />);
    expect(screen.getByTestId("main")).toHaveAttribute("data-panel-open");

    await user.click(screen.getByText("to two"));
    expect(screen.getByTestId("main")).not.toHaveAttribute("data-panel-open");
    await user.keyboard("{Escape}");
    expect(onClose).not.toHaveBeenCalled();

    await user.click(screen.getByText("to one"));
    expect(screen.getByTestId("main")).toHaveAttribute("data-panel-open");
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("coming back to the tab leaves focus alone and still returns it to the opener on close", async () => {
    function OpenedInTab() {
      const [tab, setTab] = useState("one");
      const [open, setOpen] = useState(false);
      return (
        <main tabIndex={-1}>
          <button onClick={() => setTab("two")}>to two</button>
          <button onClick={() => setTab("one")}>to one</button>
          <TabPanels
            active={tab}
            panels={[
              {
                key: "one",
                render: () => (
                  <>
                    <button onClick={() => setOpen(true)}>Open it</button>
                    <SidePanel open={open} title="Add thing" onClose={() => setOpen(false)}>
                      <input aria-label="Name" />
                    </SidePanel>
                  </>
                ),
              },
              { key: "two", render: () => <p>second tab</p> },
            ]}
          />
        </main>
      );
    }
    const user = userEvent.setup();
    render(<OpenedInTab />);
    await user.click(screen.getByText("Open it"));
    expect(screen.getByLabelText("Name")).toHaveFocus();

    await user.click(screen.getByText("to two"));
    await user.click(screen.getByText("to one"));
    expect(screen.getByText("to one")).toHaveFocus();

    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.getByText("Open it")).toHaveFocus();
  });
});
