import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { LayersProvider, useEscapeLayer, useInertShell } from "./layers";

function Layer({ active = true, onEscape }: { active?: boolean; onEscape: () => void }) {
  useEscapeLayer(active, onEscape);
  return null;
}

describe("useEscapeLayer", () => {
  it("calls only the newest layer, then the one below once it is gone", async () => {
    const user = userEvent.setup();
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = render(
      <>
        <Layer onEscape={first} />
        <Layer onEscape={second} />
      </>
    );
    await user.keyboard("{Escape}");
    expect(second).toHaveBeenCalledTimes(1);
    expect(first).not.toHaveBeenCalled();

    rerender(
      <>
        <Layer onEscape={first} />
        <Layer active={false} onEscape={second} />
      </>
    );
    await user.keyboard("{Escape}");
    expect(first).toHaveBeenCalledTimes(1);
    expect(second).toHaveBeenCalledTimes(1);
  });

  it("ignores an Escape an inner handler already took", async () => {
    const user = userEvent.setup();
    const outer = vi.fn();
    render(
      <>
        <Layer onEscape={outer} />
        <input aria-label="inner" onKeyDown={(e) => e.preventDefault()} />
      </>
    );
    await user.click(screen.getByLabelText("inner"));
    await user.keyboard("{Escape}");
    expect(outer).not.toHaveBeenCalled();
  });
});

function Inert({ id }: { id: string }) {
  useInertShell(true);
  return <span>{id}</span>;
}

describe("useInertShell", () => {
  it("makes the shell inert until the last caller is gone", async () => {
    const user = userEvent.setup();
    function Harness() {
      const [a, setA] = useState(true);
      const [b, setB] = useState(true);
      return (
        <LayersProvider shell={() => [document.getElementById("nav")!, document.getElementById("page")!]}>
          <nav id="nav" />
          <main id="page" />
          {a && <Inert id="a" />}
          {b && <Inert id="b" />}
          <button onClick={() => setA(false)}>drop a</button>
          <button onClick={() => setB(false)}>drop b</button>
        </LayersProvider>
      );
    }
    render(<Harness />);
    expect(document.getElementById("nav")).toHaveAttribute("inert");
    expect(document.getElementById("page")).toHaveAttribute("inert");
    await user.click(screen.getByText("drop a"));
    expect(document.getElementById("nav")).toHaveAttribute("inert");
    await user.click(screen.getByText("drop b"));
    expect(document.getElementById("nav")).not.toHaveAttribute("inert");
    expect(document.getElementById("page")).not.toHaveAttribute("inert");
  });
});
