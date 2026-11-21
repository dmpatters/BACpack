import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import DatesTab from "./DatesTab";
import EditOverlay from "../EditOverlay";
import { engineApi, liveApi, objectsApi } from "../../api/endpoints";
import { ApiError } from "../../api/http";
import type { BacnetObjectDTO, CalendarEntry } from "../../api/types";
import { expectNamedControls } from "../../testing/controls";

vi.mock("../../api/endpoints", () => ({
  objectsApi: { update: vi.fn() },
  engineApi: { status: vi.fn() },
  liveApi: { object: vi.fn() },
}));

const christmas: CalendarEntry = { kind: "date", year: "*", month: "12", day: "25" };

function makeObj(overrides: Partial<BacnetObjectDTO> = {}): BacnetObjectDTO {
  return {
    id: 7,
    device_id: 599002,
    object_type: "calendar",
    instance_number: 1,
    name: "HOLIDAYS",
    bacnet_writable: true,
    date_list: [christmas],
    ...overrides,
  } as BacnetObjectDTO;
}

function renderTab(obj: BacnetObjectDTO, client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  const tab = (o: BacnetObjectDTO) => (
    <QueryClientProvider client={client}>
      <DatesTab deviceId={599002} obj={o} />
    </QueryClientProvider>
  );
  return { ...render(tab(obj)), tab };
}

const clockAt = (deviceTime: string) =>
  vi.mocked(engineApi.status).mockResolvedValue([{ device_id: 599002, device_time: deviceTime } as never]);

beforeEach(() => {
  vi.mocked(objectsApi.update).mockReset().mockResolvedValue(makeObj() as never);
  clockAt("2026-10-12T08:26:00-04:00");
});

// The Add entry panel's form (its submit button shares the opener's name).
async function openAdd(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Add entry" }));
  return screen.getByRole("form", { name: "Add entry" });
}

describe("DatesTab", () => {
  it("says whether today, on the device's clock, is in the calendar", async () => {
    clockAt("2026-12-25T09:00:00-05:00");
    renderTab(makeObj());
    expect(await screen.findByText("Today, 2026-12-25 (device time)")).toBeInTheDocument();
    expect(screen.getByText("today is in calendar")).toHaveClass("bp-pill--ok");
  });

  it("says not in calendar on another day", async () => {
    renderTab(makeObj());
    expect(await screen.findByText("not in calendar")).toHaveClass("bp-pill--plain");
  });

  it("reads a running calendar's today from the device", async () => {
    vi.mocked(liveApi.object).mockResolvedValue({
      object_id: 7, object_type: "calendar", instance: 1, present_value: true, priority_array: null,
      controlling_priority: null, relinquish_default: null, status_flags: [], event_state: null,
      reliability: null, out_of_service: false, read_at: "2026-10-12T12:26:00+00:00",
    });
    renderTab(makeObj()); // the saved list (Christmas) doesn't match 2026-10-12
    expect(await screen.findByText("today is in calendar")).toHaveClass("bp-pill--ok");
  });

  it("works today out from the saved list when the device isn't running", async () => {
    vi.mocked(liveApi.object).mockRejectedValue(new ApiError("device 599002 isn't running", 409));
    renderTab(makeObj());
    expect(await screen.findByText("not in calendar")).toHaveClass("bp-pill--plain");
  });

  it("lists each entry in words and in its BACnet form", () => {
    renderTab(makeObj());
    const row = screen.getAllByRole("row")[1];
    expect(within(row).getByText("Date")).toHaveClass("bp-pill");
    expect(within(row).getByText("December 25, every year")).toBeInTheDocument();
    expect(within(row).getByText("year * · month 12 · day 25 · weekday *")).toHaveClass("bp-cell-id");
    expect(within(row).getByRole("button", { name: "Delete" })).toBeEnabled();
  });

  it("deletes the last entry and saves an empty list, which is never in effect", async () => {
    const user = userEvent.setup();
    renderTab(makeObj());
    await user.click(screen.getByRole("button", { name: "Delete" }));
    expect(screen.getByText("No dates.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(objectsApi.update).toHaveBeenCalledWith(599002, 7, { date_list: [] });
  });

  it("shows a calendar with no dates as empty, with nothing to save", async () => {
    const user = userEvent.setup();
    renderTab(makeObj({ date_list: null }));
    expect(screen.getByText("No dates.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(objectsApi.update).toHaveBeenCalledWith(599002, 7, {});
  });

  it("adds an entry to the list and saves the list", async () => {
    const user = userEvent.setup();
    renderTab(makeObj());
    expect(screen.queryByRole("dialog", { name: "Add entry" })).not.toBeInTheDocument();
    const add = await openAdd(user);
    expect(screen.getByRole("dialog", { name: "Add entry" })).toBeVisible();
    await user.click(within(add).getByRole("radio", { name: "Pattern" }));
    await user.selectOptions(within(add).getByLabelText("Which week"), "Last");
    await user.selectOptions(within(add).getByLabelText("Weekday"), "Friday");
    await user.click(within(add).getByRole("button", { name: "Add entry" }));
    expect(screen.getByText("Last Friday of every month", { selector: "td" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Add entry" })).not.toBeInTheDocument();
    expect(objectsApi.update).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(vi.mocked(objectsApi.update).mock.calls[0][2]).toEqual({
      date_list: [christmas, { kind: "week-and-day", month: "*", week_of_month: "last", day_of_week: "5" }],
    });
    expect(await screen.findByRole("status")).toHaveTextContent("Saved.");
  });

  it("edits an entry in place", async () => {
    const user = userEvent.setup();
    renderTab(makeObj());
    await user.click(screen.getByRole("button", { name: "Edit" }));
    const edit = screen.getByRole("form", { name: "Edit entry 1" });
    expect(within(edit).getByLabelText("Month")).toHaveDisplayValue("December");
    await user.selectOptions(within(edit).getByLabelText("Day"), "26");
    await user.click(within(edit).getByRole("button", { name: "Update entry" }));
    expect(screen.getByText("December 26, every year", { selector: "td" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: /entry/ })).not.toBeInTheDocument();
  });

  it("opens a row's Edit as 'Edit entry N', prefilled", async () => {
    const user = userEvent.setup();
    const day = (d: string): CalendarEntry => ({ kind: "date", year: "*", month: "1", day: d });
    renderTab(makeObj({ date_list: [day("1"), day("2")] }));
    await user.click(screen.getAllByRole("button", { name: "Edit" })[1]);
    expect(screen.getByRole("dialog", { name: "Edit entry 2" })).toBeVisible();
    expect(within(screen.getByRole("form", { name: "Edit entry 2" })).getByLabelText("Day")).toHaveDisplayValue("2");
  });

  it("offers to continue a half-edited entry instead of Add entry, and brings it back", async () => {
    const user = userEvent.setup();
    renderTab(makeObj());
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.selectOptions(within(screen.getByRole("form", { name: "Edit entry 1" })).getByLabelText("Day"), "26");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("button", { name: "Add entry" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Continue editing entry 1" }));
    expect(within(screen.getByRole("form", { name: "Edit entry 1" })).getByLabelText("Day")).toHaveDisplayValue("26");

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Add entry" })).toBeInTheDocument();
  });

  it("keeps a half-typed entry when the panel is closed, and still counts it as unsaved", async () => {
    const user = userEvent.setup();
    renderTab(makeObj());
    const add = await openAdd(user);
    await user.selectOptions(within(add).getByLabelText("Day"), "26");
    await user.click(within(screen.getByRole("dialog", { name: "Add entry" })).getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog", { name: "Add entry" })).not.toBeInTheDocument();
    const again = await openAdd(user);
    expect(within(again).getByLabelText("Day")).toHaveDisplayValue("26");
  });

  it("keeps updating the entry being edited after an earlier entry is deleted", async () => {
    const user = userEvent.setup();
    const day = (d: string): CalendarEntry => ({ kind: "date", year: "*", month: "1", day: d });
    renderTab(makeObj({ date_list: [day("1"), day("2"), day("3"), day("4")] }));
    await user.click(screen.getAllByRole("button", { name: "Edit" })[2]);
    await user.click(screen.getAllByRole("button", { name: "Delete" })[0]);
    const edit = screen.getByRole("form", { name: "Edit entry 2" });
    await user.selectOptions(within(edit).getByLabelText("Day"), "13");
    await user.click(within(edit).getByRole("button", { name: "Update entry" }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(vi.mocked(objectsApi.update).mock.calls[0][2]).toEqual({
      date_list: [day("2"), day("13"), day("4")],
    });
  });

  it("won't add a range whose end is before its start", async () => {
    const user = userEvent.setup();
    renderTab(makeObj());
    const add = await openAdd(user);
    await user.click(within(add).getByRole("radio", { name: "Date range" }));
    // jsdom drops a date typed key by key; set it whole, as a picker does.
    fireEvent.change(within(add).getByLabelText("Start date"), { target: { value: "2026-07-07" } });
    fireEvent.change(within(add).getByLabelText("End date"), { target: { value: "2026-07-01" } });
    await user.click(within(add).getByRole("button", { name: "Add entry" }));
    expect(within(add).getByText("Can't add:").closest(".bp-error")).toHaveTextContent("Can't add: End is before start.");
    expect(screen.getAllByRole("row")).toHaveLength(2);
  });

  it("says Can't update while an entry is being edited", async () => {
    const user = userEvent.setup();
    renderTab(makeObj());
    await user.click(screen.getByRole("button", { name: "Edit" }));
    const edit = screen.getByRole("form", { name: "Edit entry 1" });
    await user.click(within(edit).getByRole("radio", { name: "Date range" }));
    fireEvent.change(within(edit).getByLabelText("Start date"), { target: { value: "2026-07-07" } });
    fireEvent.change(within(edit).getByLabelText("End date"), { target: { value: "2026-07-01" } });
    await user.click(within(edit).getByRole("button", { name: "Update entry" }));
    expect(within(edit).getByText("Can't update:").closest(".bp-error")).toHaveTextContent(
      "Can't update: End is before start."
    );
  });

  it("marks a listed entry that matches no date", async () => {
    renderTab(makeObj({ date_list: [{ kind: "date", year: "2025", month: "12", day: "25" }] }));
    expect(await screen.findByText("matches no date")).toHaveClass("bp-pill--warn");
  });

  it("keeps an unsaved edit when another tab's save refreshes the object", async () => {
    const user = userEvent.setup();
    const { rerender, tab } = renderTab(makeObj());
    await user.click(screen.getByLabelText("BACnet writable"));
    rerender(tab(makeObj({ date_list: [{ kind: "date", year: "*", month: "1", day: "1" }] })));
    expect(screen.getByLabelText("BACnet writable")).not.toBeChecked();
    expect(screen.getByText("January 1, every year", { selector: "td" })).toBeInTheDocument();
  });

  it("names every control", async () => {
    const { container } = renderTab(makeObj());
    await openAdd(userEvent.setup());
    expectNamedControls(container);
  });

  it("closes the entry panel before the overlay it sits in", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <EditOverlay title="HOLIDAYS" onClose={onClose}>
          <DatesTab deviceId={599002} obj={makeObj()} />
        </EditOverlay>
      </QueryClientProvider>
    );
    await openAdd(user);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Add entry" })).not.toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
