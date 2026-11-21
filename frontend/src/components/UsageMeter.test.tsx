import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import UsageMeter, { meterTone } from "./UsageMeter";

describe("meterTone", () => {
  it.each([
    [10, "accent"],
    [80, "accent"],
    [80.1, "warn"],
    [95.1, "danger"],
  ] as const)("%s%% is %s", (pct, tone) => expect(meterTone(pct)).toBe(tone));
});

describe("UsageMeter", () => {
  it("is a named meter that states its numbers", () => {
    render(<UsageMeter label="Trend data" valueText="12.34 MB of 1024 MB" percent={1.2} />);
    const meter = screen.getByRole("meter", { name: "Trend data" });
    expect(meter).toHaveAttribute("aria-valuenow", "1");
    expect(meter).toHaveAttribute("aria-valuetext", "12.34 MB of 1024 MB");
    expect(screen.getByText("1.2%")).toHaveClass("bp-mono");
    expect(meter.querySelector<HTMLElement>(".bp-meter__fill")!.style.getPropertyValue("--value")).toBe("1.2%");
  });

  it("turns warn and danger near the limit", () => {
    const { container, rerender } = render(<UsageMeter label="x" valueText="" percent={85} />);
    expect(container.querySelector(".bp-meter__fill")).toHaveClass("bp-meter__fill--warn");
    rerender(<UsageMeter label="x" valueText="" percent={99} />);
    expect(container.querySelector(".bp-meter__fill")).toHaveClass("bp-meter__fill--danger");
  });

  it("has a small variant for table cells with no head", () => {
    render(<UsageMeter label="AV-1 share" valueText="40%" percent={40} small />);
    const meter = screen.getByRole("meter", { name: "AV-1 share" });
    expect(meter).toHaveClass("bp-meter", "bp-meter--sm");
    expect(meter.querySelector(".bp-meter__head")).toBeNull();
  });
});
