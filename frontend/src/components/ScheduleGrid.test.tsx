import { useState } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import ScheduleGrid from "./ScheduleGrid";
import type { ScheduleDaySchedule } from "../api/types";
import type { ValueNaming } from "../utils/scheduleGrid";
import { expectNamedControls } from "../testing/controls";

const naming: ValueNaming = { family: "binary", states: [], unit: "", defaultValue: "inactive" };
const workday = () => [
  { time: "08:00", value: "active" },
  { time: "17:00", value: null },
];
const initialWeek = (): ScheduleDaySchedule[] => [workday(), workday(), [], [], [], [], []];

function Harness({ initial = initialWeek(), deviceTime = null }: { initial?: ScheduleDaySchedule[]; deviceTime?: string | null }) {
  const [days, setDays] = useState(initial);
  return (
    <>
      <ScheduleGrid days={days} onChange={setDays} naming={naming} deviceTime={deviceTime} />
      <output data-testid="days">{JSON.stringify(days)}</output>
    </>
  );
}

const stored = (): ScheduleDaySchedule[] => JSON.parse(screen.getByTestId("days").textContent ?? "[]");
const row = (day: string) => screen.getByRole("button", { name: day }).closest(".bp-sched__row") as HTMLElement;

describe("ScheduleGrid", () => {
  it("draws each day on one 24-hour track with its entries as text", () => {
    render(<Harness />);
    expect(screen.getByRole("group", { name: "Weekly schedule, device time" })).toHaveClass("bp-sched");
    const monday = row("Monday");
    const block = monday.querySelector(".bp-sched__seg") as HTMLElement;
    expect(block).toHaveTextContent("Active");
    expect(block.style.getPropertyValue("--start")).toBe("33.333%");
    expect(block.style.getPropertyValue("--span")).toBe("37.500%");
    expect(within(monday).getByText("08:00 Active · 17:00 Inactive (default)")).toBeInTheDocument();
    expect(within(row("Sunday")).getByText("Inactive all day (default)")).toBeInTheDocument();
  });

  it("edits the selected day, Monday first", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    expect(screen.getByRole("button", { name: "Monday" })).toHaveAttribute("aria-pressed", "true");
    expect(row("Monday")).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("heading", { name: "Monday" })).toBeInTheDocument();
    expect(screen.getByLabelText("Value, entry 2")).toHaveDisplayValue("Default (Inactive)");
    await user.click(screen.getByRole("button", { name: "Wednesday" }));
    expect(screen.getByRole("heading", { name: "Wednesday" })).toBeInTheDocument();
    expect(screen.getByText("No entries: Inactive all day.")).toBeInTheDocument();
  });

  it("adds, changes and deletes entries of the selected day", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Add entry" }));
    expect(stored()[0][2]).toEqual({ time: "18:00", value: "active" });
    await user.selectOptions(screen.getByLabelText("Value, entry 3"), "Inactive");
    expect(stored()[0][2].value).toBe("inactive");
    await user.click(screen.getByRole("button", { name: "Delete entry 1" }));
    expect(stored()[0]).toEqual([
      { time: "17:00", value: null },
      { time: "18:00", value: "inactive" },
    ]);
  });

  it("echoes an entry's time in device time", () => {
    render(<Harness />);
    const time = screen.getByLabelText("Time, entry 1");
    fireEvent.change(time, { target: { value: "08:05" } });
    expect(within(time.closest(".bp-sched__entry") as HTMLElement).getByTestId("iso-echo")).toHaveTextContent(
      "08:05 device time"
    );
  });

  it("flags an entry that repeats a time", () => {
    render(<Harness />);
    const second = screen.getByLabelText("Time, entry 2");
    // jsdom drops a time typed key by key; set it whole, as a picker does.
    fireEvent.change(second, { target: { value: "08:00" } });
    expect(screen.getByRole("alert")).toHaveTextContent("Entry 2 repeats 08:00.");
    expect(second).toHaveAttribute("aria-invalid", "true");
  });

  it("copies the selected day to the chosen days, replacing theirs", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Weekdays" }));
    const picks = screen.getByRole("group", { name: "Copy Monday to" });
    expect(within(picks).getByRole("button", { name: "Copy to Friday" })).toHaveAttribute("aria-pressed", "true");
    expect(within(picks).getByRole("button", { name: "Copy to Saturday" })).toHaveAttribute("aria-pressed", "false");
    await user.click(within(picks).getByRole("button", { name: "Copy to Thursday" }));
    await user.click(within(picks).getByRole("button", { name: "Copy" }));
    const days = stored();
    expect(days[2]).toEqual(workday());
    expect(days[3]).toEqual([]);
    expect(days[4]).toEqual(workday());
    expect(screen.getByText("Copied Monday to Tuesday, Wednesday and Friday.")).toHaveAttribute("role", "status");
  });

  it("marks the device's time on today's row", () => {
    render(<Harness deviceTime="2026-10-14T08:26:00-04:00" />);
    const marker = row("Wednesday").querySelector(".bp-sched__now") as HTMLElement;
    expect(marker.style.getPropertyValue("--at")).toBe("35.139%");
    expect(screen.getByText("Device time · now 08:26")).toBeInTheDocument();
    expect(row("Monday").querySelector(".bp-sched__now")).toBeNull();
  });

  it("names every control", () => {
    const { container } = render(<Harness />);
    expectNamedControls(container);
  });
});
