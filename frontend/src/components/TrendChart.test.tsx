import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import TrendChart from "./TrendChart";
import type { TrendRangeDTO } from "../api/types";

const START = 1_800_000_000; // 2027-01-15 08:00 UTC
const END = START + 3600;
const iso = (t: number) => new Date(t * 1000).toISOString();

function range(overrides: Partial<TrendRangeDTO> = {}): TrendRangeDTO {
  const samples = [0, 600, 1200, 1800].map((s, i) => ({ timestamp: iso(START + s), value: 70 + i }));
  return {
    mode: "interval",
    interval_seconds: 600,
    step: false,
    samples,
    carry_in: null,
    gaps: [],
    thinned: false,
    count_in_range: samples.length,
    earliest_sample: iso(START - 86400),
    start: iso(START),
    end: iso(END),
    ...overrides,
  };
}

function draw(r: TrendRangeDTO, extra: Partial<Parameters<typeof TrendChart>[0]> = {}) {
  return render(
    <TrendChart
      range={r}
      start={START}
      end={END}
      title="Zone temp"
      sub="analog-value:1 · °F"
      unit="°F"
      format={(v) => String(v)}
      {...extra}
    />
  );
}

describe("TrendChart", () => {
  it("draws a titled chart with an accessible picture and a footer", () => {
    const { container } = draw(range());
    expect(screen.getByRole("heading", { name: /Zone temp/ })).toBeInTheDocument();
    expect(screen.getByText("analog-value:1 · °F")).toHaveClass("bp-chart__sub");
    expect(screen.getByRole("img", { name: /Zone temp.*latest 73 °F/ })).toHaveClass("bp-chart__svg");
    expect(container.querySelector(".bp-chart__foot")).toHaveTextContent("4 samples · every 600 s");
  });

  it("breaks the line at a gap and names it in the footer", () => {
    const { container } = draw(range({ gaps: [{ start: iso(START + 600), end: iso(START + 1200) }] }));
    expect(container.querySelectorAll(".bp-chart__line")).toHaveLength(2);
    expect(container.querySelectorAll(".bp-chart__area")).toHaveLength(2);
    expect(container.querySelector(".bp-chart__foot")).toHaveTextContent("no samples 08:10–08:20");
  });

  it("draws a step line with state names, and gives a value outside them its own band", () => {
    const samples = [1, 2, 7].map((v, i) => ({ timestamp: iso(START + i * 600), value: v }));
    const { container } = draw(range({ step: true, samples, count_in_range: 3 }), {
      levels: [
        { value: 1, label: "Off" },
        { value: 2, label: "Low" },
        { value: 3, label: "High" },
      ],
    });
    expect(container.querySelector(".bp-chart__area")).toBeNull();
    const labels = [...container.querySelectorAll("text.bp-chart__text")].map((t) => t.textContent);
    expect(labels).toEqual(expect.arrayContaining(["Off", "Low", "High", "7 · not a state"]));
    expect(container.querySelector(".bp-chart__line")?.getAttribute("d")).toMatch(/^M[\d.]+,[\d.]+ L/);
  });

  it("draws two states with the same name without a duplicate key", () => {
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    const samples = [1, 2].map((v, i) => ({ timestamp: iso(START + i * 600), value: v }));
    const { container } = draw(range({ step: true, samples, count_in_range: 2 }), {
      levels: [
        { value: 1, label: "Low" },
        { value: 2, label: "Low" },
      ],
    });
    const axis = [...container.querySelectorAll("text.bp-chart__text")].filter((t) => t.textContent === "Low");
    expect(axis).toHaveLength(2);
    expect(error).not.toHaveBeenCalled();
    error.mockRestore();
  });

  it("says a COV trend holds its value", () => {
    const { container } = draw(range({ mode: "cov", interval_seconds: null }));
    expect(container.querySelector(".bp-chart__foot")).toHaveTextContent(
      "Change-of-value trend"
    );
  });

  it("draws one sample without NaN", () => {
    const one = [{ timestamp: iso(START + 60), value: 5 }];
    const { container } = draw(range({ samples: one, count_in_range: 1 }));
    expect(container.querySelectorAll(".bp-chart__dot")).toHaveLength(1);
    for (const el of container.querySelectorAll("[d],[cx],[cy],[x],[y]")) {
      for (const attr of ["d", "cx", "cy", "x", "y"]) expect(el.getAttribute(attr) ?? "").not.toContain("NaN");
    }
  });

  it("leaves out samples after the range's end, so nothing lands in the label margin", () => {
    const r = range();
    const { container } = draw({ ...r, samples: [...r.samples, { timestamp: iso(END + 120), value: 99 }] });
    expect(screen.getByRole("img", { name: /latest 73 °F/ })).toBeInTheDocument();
    expect(container.querySelector(".bp-chart__label")).toHaveTextContent("73 °F");
  });

  it("labels only the latest value, with its unit", () => {
    const { container } = draw(range());
    const labels = container.querySelectorAll(".bp-chart__label");
    expect(labels).toHaveLength(1);
    expect(labels[0]).toHaveTextContent("73 °F");
  });

  it("moves a tooltip between samples with the keyboard", async () => {
    const user = userEvent.setup();
    const { container } = draw(range());
    await user.tab(); // "View as table", in the head
    await user.tab();
    const plot = screen.getByLabelText("Samples; arrow keys move between them");
    expect(plot).toHaveFocus();
    const tip = screen.getByRole("tooltip");
    expect(within(tip).getByText("73 °F")).toHaveClass("bp-tip__value");
    expect(within(tip).getByText("2027-01-15 08:30:00 UTC")).toHaveClass("bp-tip__time");
    await user.keyboard("{ArrowLeft}");
    expect(within(screen.getByRole("tooltip")).getByText("72 °F")).toBeInTheDocument();
    await user.keyboard("{Home}");
    expect(within(screen.getByRole("tooltip")).getByText("70 °F")).toBeInTheDocument();
    await user.keyboard("{End}");
    expect(within(screen.getByRole("tooltip")).getByText("73 °F")).toBeInTheDocument();
    expect(container.querySelector(".bp-chart__cross")).not.toBeNull();
  });

  it("swaps in a table, newest first", async () => {
    const user = userEvent.setup();
    draw(range());
    await user.click(screen.getByRole("button", { name: "View as table" }));
    const table = screen.getByRole("table", { name: "Trend samples" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("2027-01-15 08:30:00");
    expect(rows[0]).toHaveTextContent("73");
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "View as chart" })).toBeInTheDocument();
  });

  it("says when the chart is thinned and when history starts inside the range", () => {
    const { container } = draw(range({ thinned: true, count_in_range: 9000, earliest_sample: iso(START + 300) }));
    const foot = container.querySelector(".bp-chart__foot");
    expect(foot).toHaveTextContent("showing 4 of 9000; export CSV for all");
    expect(foot).toHaveTextContent("history starts 2027-01-15 08:05:00 UTC");
  });

  it("dims a stale chart", () => {
    const { container } = draw(range(), { stale: true });
    expect(container.querySelector("section")).toHaveClass("bp-chart--stale");
  });

  it("says so when the range has no samples", () => {
    draw(range({ samples: [], count_in_range: 0 }));
    expect(screen.getByText("No samples in this range.")).toBeInTheDocument();
  });
});
