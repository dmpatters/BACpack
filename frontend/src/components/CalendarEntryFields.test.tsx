import { useState } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import CalendarEntryFields from "./CalendarEntryFields";
import type { CalendarEntry } from "../api/types";
import { expectNamedControls } from "../testing/controls";

const today = { year: 2026, month: 10, day: 12 };

function Harness({ initial, calendars }: { initial: CalendarEntry; calendars?: { instance_number: number; name: string }[] }) {
  const [entry, setEntry] = useState(initial);
  return (
    <>
      <CalendarEntryFields idPrefix="t" entry={entry} onChange={setEntry} today={today} calendars={calendars} />
      <output data-testid="entry">{JSON.stringify(entry)}</output>
    </>
  );
}

const stored = () => JSON.parse(screen.getByTestId("entry").textContent ?? "{}");

describe("CalendarEntryFields", () => {
  it("offers wildcards as words, never a text box", () => {
    render(<Harness initial={{ kind: "date", year: "*", month: "*", day: "*" }} />);
    expect(screen.getByLabelText("Year")).toHaveDisplayValue("Every year");
    expect(screen.getByLabelText("Month")).toHaveDisplayValue("Every month");
    expect(screen.getByLabelText("Day")).toHaveDisplayValue("Every day");
    expect(screen.getByLabelText("Weekday")).toHaveDisplayValue("Any weekday");
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("stores a chosen month as its number", async () => {
    const user = userEvent.setup();
    render(<Harness initial={{ kind: "date", year: "*", month: "*", day: "*" }} />);
    await user.selectOptions(screen.getByLabelText("Month"), "December");
    await user.selectOptions(screen.getByLabelText("Day"), "25");
    expect(stored()).toMatchObject({ kind: "date", month: "12", day: "25" });
    expect(screen.getByText("December 25, every year")).toBeInTheDocument();
  });

  it("describes a pattern and lists its next three dates", async () => {
    const user = userEvent.setup();
    render(<Harness initial={{ kind: "date", year: "*", month: "*", day: "*" }} />);
    await user.click(screen.getByRole("radio", { name: "Pattern" }));
    await user.selectOptions(screen.getByLabelText("Which week"), "Last");
    await user.selectOptions(screen.getByLabelText("Weekday"), "Friday");
    expect(stored()).toEqual({ kind: "week-and-day", month: "*", week_of_month: "last", day_of_week: "5" });
    expect(screen.getByText("Last Friday of every month")).toBeInTheDocument();
    expect(screen.getByText("Next: 2026-10-30 · 2026-11-27 · 2026-12-25")).toBeInTheDocument();
  });

  it("keeps a kind's values when switching away and back", async () => {
    const user = userEvent.setup();
    render(<Harness initial={{ kind: "date", year: "*", month: "*", day: "*" }} />);
    await user.selectOptions(screen.getByLabelText("Month"), "December");
    await user.click(screen.getByRole("radio", { name: "Date range" }));
    expect(screen.queryByLabelText("Month")).not.toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: "Date" }));
    expect(screen.getByLabelText("Month")).toHaveDisplayValue("December");
  });

  it("forgets the remembered kinds when the entry is replaced from outside", async () => {
    const user = userEvent.setup();
    function Resettable() {
      const [entry, setEntry] = useState<CalendarEntry>({ kind: "date", year: "*", month: "*", day: "*" });
      return (
        <>
          <CalendarEntryFields idPrefix="t" entry={entry} onChange={setEntry} today={today} />
          <button type="button" onClick={() => setEntry({ kind: "date", year: "*", month: "*", day: "*" })}>
            Reset
          </button>
        </>
      );
    }
    render(<Resettable />);
    await user.click(screen.getByRole("radio", { name: "Date range" }));
    fireEvent.change(screen.getByLabelText("Start date"), { target: { value: "2026-12-20" } });
    await user.click(screen.getByRole("radio", { name: "Date" }));
    await user.click(screen.getByRole("button", { name: "Reset" }));
    await user.click(screen.getByRole("radio", { name: "Date range" }));
    expect(screen.getByLabelText("Start date")).toHaveValue("");
  });

  it("warns about an entry that matches no date", () => {
    render(<Harness initial={{ kind: "date", year: "*", month: "2", day: "30" }} />);
    expect(screen.getByText("No date from today on matches it.")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("This entry matches no date from today on.");
  });

  it("echoes a range's dates in ISO form", () => {
    render(<Harness initial={{ kind: "date-range", start: "2026-07-01", end: "2026-07-07" }} />);
    const start = screen.getByLabelText("Start date");
    fireEvent.change(start, { target: { value: "2026-10-16" } });
    expect(within(start.closest(".bp-field") as HTMLElement).getByTestId("iso-echo")).toHaveTextContent(/^2026-10-16$/);
  });

  it("marks a range whose end is before its start", () => {
    render(<Harness initial={{ kind: "date-range", start: "2026-07-07", end: "2026-07-01" }} />);
    const end = screen.getByLabelText("End date");
    expect(end).toHaveAttribute("aria-invalid", "true");
    expect(end).toHaveAccessibleDescription("End is before start.");
  });

  it("offers a calendar reference only where calendars are given", async () => {
    const user = userEvent.setup();
    const { unmount } = render(<Harness initial={{ kind: "date", year: "*", month: "*", day: "*" }} />);
    expect(screen.queryByRole("radio", { name: "Calendar" })).not.toBeInTheDocument();
    unmount();
    render(<Harness initial={{ kind: "date", year: "*", month: "*", day: "*" }} calendars={[{ instance_number: 3, name: "HOLIDAYS" }]} />);
    await user.click(screen.getByRole("radio", { name: "Calendar" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Calendar" }), "HOLIDAYS");
    expect(stored()).toEqual({ kind: "calendar-reference", instance_number: 3 });
    expect(screen.getByText("Whenever calendar HOLIDAYS is in effect")).toBeInTheDocument();
  });

  it("names every control in each kind", async () => {
    const user = userEvent.setup();
    const { container } = render(<Harness initial={{ kind: "date", year: "*", month: "*", day: "*" }} calendars={[]} />);
    for (const kind of ["Date", "Date range", "Pattern", "Calendar"]) {
      await user.click(screen.getByRole("radio", { name: kind }));
      expectNamedControls(container);
    }
  });
});
