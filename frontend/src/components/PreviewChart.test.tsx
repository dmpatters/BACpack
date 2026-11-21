import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import PreviewChart from "./PreviewChart";
import { simulatePreview } from "../utils/simPreview";

const now = 1_800_000_000; // 08:00 UTC

describe("PreviewChart", () => {
  it("draws two levels with the same name without a duplicate key", () => {
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    const series = simulatePreview("binary", { kind: "square", period_s: 120, duty: 0.5 }, now)!;
    render(
      <PreviewChart
        series={series}
        label="Preview"
        levels={[
          { value: 0, label: "Same" },
          { value: 1, label: "Same" },
        ]}
      />
    );
    expect(screen.getAllByText("Same")).toHaveLength(2);
    expect(error).not.toHaveBeenCalled();
    error.mockRestore();
  });

  it("draws a line with its area, value ticks, UTC times and the value now", () => {
    const series = simulatePreview("analog", { kind: "sine", center: 50, amplitude: 10, period_s: 600, interval_s: 5, decimals: 2 }, now)!;
    const { container } = render(<PreviewChart series={series} label="Preview of a sine" unit="°F" format={(v) => v.toFixed(2)} />);
    expect(screen.getByRole("img", { name: "Preview of a sine" })).toHaveClass("bp-chart__svg");
    for (const text of ["40", "50", "60", "08:00", "08:20", "time (UTC)", "now 50.00 °F"]) {
      expect(screen.getByText(text)).toBeInTheDocument();
    }
    expect(container.querySelector(".bp-chart__area")).not.toBeNull();
    expect(container.querySelector(".bp-chart__line")?.getAttribute("d")).toMatch(/^M64(\.0+)?,/);
    expect(container.querySelector(".bp-chart__dot")).not.toBeNull();
  });

  it("draws a step line with named levels and no area", () => {
    const series = simulatePreview("binary", { kind: "square", period_s: 120, duty: 0.5 }, now)!;
    const { container } = render(
      <PreviewChart
        series={series}
        label="Preview of a square wave"
        levels={[
          { value: 0, label: "Inactive" },
          { value: 1, label: "Active" },
        ]}
      />
    );
    expect(screen.getByText("Inactive")).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(container.querySelector(".bp-chart__area")).toBeNull();
    expect(container.querySelector(".bp-chart__label")).toBeNull();
    // A step: each change is a vertical move at one x.
    const d = container.querySelector(".bp-chart__line")?.getAttribute("d") ?? "";
    const xs = [...d.matchAll(/[ML]([\d.]+),/g)].map((m) => m[1]);
    expect(xs[1]).toBe(xs[2]);
  });
});
