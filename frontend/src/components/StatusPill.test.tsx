import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LiveApplyPills, LogLevelPill, ProcessStatusPill, ResultPill } from "./StatusPill";

describe("ProcessStatusPill", () => {
  it.each([
    ["running", "running", "bp-pill--ok"],
    ["crashed", "crashed", "bp-pill--danger"],
  ] as const)("%s reads %s with a colour", (status, text, cls) => {
    render(<ProcessStatusPill status={status} />);
    expect(screen.getByText(text)).toHaveClass("bp-pill", cls);
  });

  it("stopped is neutral", () => {
    render(<ProcessStatusPill status="stopped" />);
    expect(screen.getByText("stopped").className).toBe("bp-pill");
  });

  it.each(["starting", "stopping", "reinitializing"] as const)(
    "%s is neutral, with an ellipsis, announced",
    (status) => {
      render(<ProcessStatusPill status={status} />);
      const pill = screen.getByText(`${status}…`);
      expect(pill.className).toBe("bp-pill");
      expect(pill).toHaveAttribute("aria-live", "polite");
    }
  );
});

describe("ResultPill", () => {
  it("is a dash before any result, ok, or error", () => {
    const { rerender } = render(<ResultPill error={undefined} />);
    expect(screen.getByText("—")).toBeInTheDocument();
    rerender(<ResultPill error={null} />);
    expect(screen.getByText("ok")).toHaveClass("bp-pill", "bp-pill--ok");
    rerender(<ResultPill error="unknown-object" />);
    expect(screen.getByText("error")).toHaveClass("bp-pill", "bp-pill--danger");
  });
});

describe("LogLevelPill", () => {
  it.each([
    ["DEBUG", null],
    ["INFO", "bp-pill--info"],
    ["WARNING", "bp-pill--warn"],
    ["ERROR", "bp-pill--danger"],
    ["CRITICAL", "bp-pill--danger"],
  ] as const)("%s", (level, tone) => {
    render(<LogLevelPill level={level} />);
    const pill = screen.getByText(level);
    expect(pill).toHaveClass("bp-pill", "bp-pill--plain");
    if (tone) expect(pill).toHaveClass(tone);
    else expect(pill.className).toBe("bp-pill bp-pill--plain");
  });
});

describe("LiveApplyPills", () => {
  it("shows restart pending and apply failed, or nothing", () => {
    const { container, rerender } = render(<LiveApplyPills status={{ restart_pending: false, apply_error: null }} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<LiveApplyPills status={{ restart_pending: true, apply_error: "bad row" }} />);
    expect(screen.getByText("restart pending")).toHaveClass("bp-pill--warn");
    expect(screen.getByText("apply failed")).toHaveClass("bp-pill--danger");
  });
});
