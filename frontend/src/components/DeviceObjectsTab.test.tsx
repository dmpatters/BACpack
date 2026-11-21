import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import DeviceObjectsTab from "./DeviceObjectsTab";
import { objectsApi, unitsApi } from "../api/endpoints";
import { expectNamedControls } from "../testing/controls";

vi.mock("../api/endpoints", () => ({
  objectsApi: { list: vi.fn(), remove: vi.fn(), batchUpdate: vi.fn(), batchDelete: vi.fn(), create: vi.fn() },
  unitsApi: { list: vi.fn().mockResolvedValue({ units: [] }) },
}));

const base = {
  device_id: 1,
  description: null,
  units: null,
  bacnet_writable: true,
  trended: false,
  alarming_enabled: false,
  out_of_service_default: false,
};
const a = { ...base, id: 10, object_type: "analog-value", instance_number: 1, name: "AV-A" };
const b = { ...base, id: 11, object_type: "analog-value", instance_number: 2, name: "AV-B" };
const c = { ...base, id: 12, object_type: "binary-value", instance_number: 1, name: "Pump run", description: "Zone pump" };

function Where() {
  const location = useLocation();
  return <p data-testid="where">{`${location.pathname} ${JSON.stringify(location.state)}`}</p>;
}

function renderTab() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Where />
        <main tabIndex={-1}>
          <DeviceObjectsTab deviceId={1} />
        </main>
      </MemoryRouter>
    </QueryClientProvider>
  );
  return userEvent.setup();
}

beforeEach(() => vi.clearAllMocks());

describe("DeviceObjectsTab", () => {
  // A deleted id must leave the selection, or the next bulk action 404s.
  it("drops a row deleted on its own from the selection", async () => {
    vi.mocked(objectsApi.list).mockResolvedValueOnce([a, b] as never).mockResolvedValue([b] as never);
    vi.mocked(objectsApi.remove).mockResolvedValue(undefined as never);
    vi.mocked(objectsApi.batchDelete).mockResolvedValue({ deleted: 1 } as never);
    const user = renderTab();
    await user.click(await screen.findByLabelText("Select AV-A"));
    await user.click(screen.getByLabelText("Select AV-B"));
    const rowA = screen.getByText("AV-A").closest("tr")!;
    await user.click(within(rowA).getByRole("button", { name: "Delete" }));
    await user.click(screen.getByRole("button", { name: "Delete object" }));
    expect(await screen.findByText("1 selected")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Delete selected" }));
    await user.click(screen.getByRole("button", { name: "Delete object" }));
    expect(objectsApi.batchDelete).toHaveBeenCalledWith(1, [11]);
  });

  it("asks before deleting, and deletes nothing on Cancel", async () => {
    vi.mocked(objectsApi.list).mockResolvedValue([a, b] as never);
    const user = renderTab();
    const rowA = (await screen.findByText("AV-A")).closest("tr")!;
    await user.click(within(rowA).getByRole("button", { name: "Delete" }));
    expect(screen.getByRole("alertdialog", { name: "Delete AV-A?" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(objectsApi.remove).not.toHaveBeenCalled();
  });

  it("searches by name, id or description, and filters by type", async () => {
    vi.mocked(objectsApi.list).mockResolvedValue([a, b, c] as never);
    const user = renderTab();
    await screen.findByText("AV-A");
    expect(screen.getByText("3 objects")).toBeInTheDocument();
    await user.type(screen.getByRole("searchbox", { name: "Search objects" }), "zone");
    expect(screen.queryByText("AV-A")).not.toBeInTheDocument();
    expect(screen.getByText("Pump run")).toBeInTheDocument();
    expect(screen.getByText("1 of 3 objects")).toBeInTheDocument();
    await user.clear(screen.getByRole("searchbox", { name: "Search objects" }));
    await user.type(screen.getByRole("searchbox", { name: "Search objects" }), "analog-value:2");
    expect(screen.getByText("AV-B")).toBeInTheDocument();
    expect(screen.queryByText("AV-A")).not.toBeInTheDocument();
    await user.clear(screen.getByRole("searchbox", { name: "Search objects" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Object type" }), "binary-value");
    expect(screen.getAllByRole("row")).toHaveLength(2); // header + Pump run
  });

  it("sorts by name from the keyboard", async () => {
    vi.mocked(objectsApi.list).mockResolvedValue([b, c, a] as never);
    const user = renderTab();
    await screen.findByText("AV-A");
    const nameButton = screen.getByRole("button", { name: "Name" });
    nameButton.focus();
    await user.keyboard("{Enter}");
    const names = () => screen.getAllByRole("row").slice(1).map((r) => within(r).getAllByRole("cell")[2].textContent);
    expect(names()).toEqual(["AV-A", "AV-B", "Pump run"]);
    expect(nameButton.closest("th")).toHaveAttribute("aria-sort", "ascending");
    await user.keyboard("{Enter}");
    expect(names()).toEqual(["Pump run", "AV-B", "AV-A"]);
  });

  it("shows Select all as mixed while only some rows are selected", async () => {
    vi.mocked(objectsApi.list).mockResolvedValue([a, b] as never);
    const user = renderTab();
    const all = (await screen.findByLabelText("Select all objects")) as HTMLInputElement;
    expect(all.indeterminate).toBe(false);
    await user.click(screen.getByLabelText("Select AV-A"));
    expect(all.indeterminate).toBe(true);
    expect(all).not.toBeChecked();
    await user.click(screen.getByLabelText("Select AV-B"));
    expect(all.indeterminate).toBe(false);
    expect(all).toBeChecked();
  });

  it("acts only on selected rows the search still shows", async () => {
    vi.mocked(objectsApi.list).mockResolvedValue([a, b, c] as never);
    vi.mocked(objectsApi.batchDelete).mockResolvedValue({ deleted: 1 } as never);
    const user = renderTab();
    await user.click(await screen.findByLabelText("Select all objects"));
    expect(screen.getByText("3 selected")).toBeInTheDocument();
    await user.type(screen.getByRole("searchbox", { name: "Search objects" }), "pump");
    expect(screen.getByText("1 selected")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Delete selected" }));
    await user.click(screen.getByRole("button", { name: "Delete object" }));
    expect(objectsApi.batchDelete).toHaveBeenCalledWith(1, [12]);
  });

  it("keeps the row and the selection when a delete fails", async () => {
    vi.mocked(objectsApi.list).mockResolvedValue([a, b] as never);
    vi.mocked(objectsApi.remove).mockRejectedValue(new Error("HTTP 409: in use"));
    const user = renderTab();
    await user.click(await screen.findByLabelText("Select AV-A"));
    const rowA = screen.getByText("AV-A").closest("tr")!;
    await user.click(within(rowA).getByRole("button", { name: "Delete" }));
    await user.click(screen.getByRole("button", { name: "Delete object" }));
    expect(await screen.findByText(/HTTP 409: in use/)).toBeInTheDocument();
    expect(screen.getByText("AV-A")).toBeInTheDocument();
    expect(screen.getByText("1 selected")).toBeInTheDocument();
  });

  it("stops simulating the selected objects", async () => {
    vi.mocked(objectsApi.list).mockResolvedValue([a, b] as never);
    vi.mocked(objectsApi.batchUpdate).mockResolvedValue([a, b] as never);
    const user = renderTab();
    await user.click(await screen.findByLabelText("Select AV-A"));
    await user.click(screen.getByLabelText("Select AV-B"));
    await user.click(screen.getByRole("button", { name: "Stop simulating" }));
    expect(objectsApi.batchUpdate).toHaveBeenCalledWith(1, [10, 11], { simulation: null }, {});
  });

  it("puts a bulk edit's error under its Apply button, not in the page banner", async () => {
    vi.mocked(objectsApi.list).mockResolvedValue([a, b] as never);
    vi.mocked(objectsApi.batchUpdate).mockRejectedValue(new Error("units: not a BACnet unit"));
    const user = renderTab();
    await user.click(await screen.findByLabelText("Select AV-A"));
    await user.click(screen.getByLabelText("Select AV-B"));
    await user.click(screen.getByRole("button", { name: "Edit selected" }));
    await user.click(screen.getByRole("checkbox", { name: "Set description" }));
    const apply = screen.getByRole("button", { name: "Apply to 2 objects" });
    await user.click(apply);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Couldn't apply the edit: units: not a BACnet unit");
    expect(screen.getByRole("dialog", { name: "Edit 2 objects at once" })).toContainElement(alert);
    expect(screen.getAllByRole("alert")).toHaveLength(1);
  });

  it("simulates the selected objects with a random phase", async () => {
    vi.mocked(objectsApi.list).mockResolvedValue([a, b] as never);
    vi.mocked(objectsApi.batchUpdate).mockResolvedValue([a, b] as never);
    const user = renderTab();
    await user.click(await screen.findByLabelText("Select AV-A"));
    await user.click(screen.getByLabelText("Select AV-B"));
    await user.click(screen.getByRole("button", { name: "Simulate selected" }));
    await user.click(screen.getByRole("button", { name: "Simulate 2 objects" }));
    expect(objectsApi.batchUpdate).toHaveBeenCalledWith(
      1,
      [10, 11],
      { simulation: { kind: "sine", center: 50, amplitude: 10, period_s: 600, interval_s: 5, decimals: 2 } },
      { randomPhase: true }
    );
  });

  it("shows no toolbar while the list is loading", async () => {
    vi.mocked(objectsApi.list).mockReturnValue(new Promise(() => {}));
    renderTab();
    expect(await screen.findByText("Loading objects…")).toBeInTheDocument();
    expect(screen.queryByRole("searchbox")).not.toBeInTheDocument();
  });

  it("shows no toolbar when the list failed, and offers a retry", async () => {
    vi.mocked(objectsApi.list).mockRejectedValue(new Error("HTTP 500"));
    renderTab();
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load objects: HTTP 500");
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.queryByRole("searchbox")).not.toBeInTheDocument();
  });
});

describe("Add object form", () => {
  beforeEach(() => {
    vi.mocked(objectsApi.list).mockResolvedValue([] as never);
    vi.mocked(objectsApi.create).mockResolvedValue({ ...base, id: 99, object_type: "binary-value", instance_number: 1, name: "BV-1" } as never);
    vi.mocked(unitsApi.list).mockResolvedValue({ units: ["degreesFahrenheit", "percent"] } as never);
  });

  const openAdd = async (user: ReturnType<typeof userEvent.setup>) => {
    await user.click(await screen.findByRole("button", { name: "Add object" }));
    return screen.getByRole("form", { name: "Add object" });
  };

  it("starts with the add panel closed", async () => {
    renderTab();
    expect(await screen.findByRole("button", { name: "Add object" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Instance number")).not.toBeInTheDocument();
  });

  it.each([
    ["analog-value", { units: true, relinquish: true, states: false }],
    ["analog-input", { units: true, relinquish: false, states: false }],
    ["binary-output", { units: false, relinquish: true, states: false }],
    ["multi-state-value", { units: false, relinquish: true, states: true }],
    ["calendar", { units: false, relinquish: false, states: false }],
    ["notification-class", { units: false, relinquish: false, states: false }],
  ])("shows only a %s's fields", async (type, shown) => {
    const user = userEvent.setup();
    renderTab();
    const form = await openAdd(user);
    await user.selectOptions(within(form).getByLabelText("Type"), type);
    expect(!!within(form).queryByRole("combobox", { name: "Units" })).toBe(shown.units);
    expect(!!within(form).queryByLabelText("Relinquish default")).toBe(shown.relinquish);
    expect(!!within(form).queryByRole("list", { name: "State names" })).toBe(shown.states);
    expect(within(form).queryByText(/only\)/)).not.toBeInTheDocument();
  });

  it("keeps a hidden field's value but doesn't send it", async () => {
    const user = userEvent.setup();
    renderTab();
    const form = await openAdd(user);
    await user.selectOptions(within(form).getByLabelText("Type"), "analog-value");
    await user.type(within(form).getByRole("combobox", { name: "Units" }), "fahr");
    await user.keyboard("{Enter}");
    await user.selectOptions(within(form).getByLabelText("Type"), "binary-value");
    await user.type(within(form).getByLabelText("Name"), "BV-1");
    await user.click(within(form).getByRole("button", { name: "Add object" }));
    expect(vi.mocked(objectsApi.create).mock.calls[0][1]).not.toHaveProperty("units");
    await user.selectOptions(within(form).getByLabelText("Type"), "analog-value");
    expect(within(form).getByRole("combobox", { name: "Units" })).toHaveValue("Degrees Fahrenheit (°F)");
  });

  it("adds a multi-state object with named states and a relinquish default chosen by name", async () => {
    vi.mocked(objectsApi.create).mockResolvedValue({ ...a, id: 20, name: "MSV-1" } as never);
    const user = userEvent.setup();
    renderTab();
    const form = await openAdd(user);
    await user.selectOptions(within(form).getByLabelText("Type"), "multi-state-value");
    await user.click(within(form).getByRole("button", { name: "Off / Low / High" }));
    await user.type(within(form).getByLabelText("Name"), "MSV-1");
    await user.selectOptions(within(form).getByLabelText("Relinquish default"), "2 · Low");
    await user.click(within(form).getByRole("button", { name: "Add object" }));
    const body = vi.mocked(objectsApi.create).mock.calls[0][1];
    expect(body).toMatchObject({ state_text: ["Off", "Low", "High"], relinquish_default: "2" });
  });

  it("drops the added line once the next object is being typed", async () => {
    const user = userEvent.setup();
    renderTab();
    const form = await openAdd(user);
    await user.selectOptions(within(form).getByLabelText("Type"), "binary-value");
    await user.type(within(form).getByLabelText("Name"), "BV-1");
    await user.click(within(form).getByRole("button", { name: "Add object" }));
    expect(await within(form).findByText("Added BV-1.")).toBeInTheDocument();
    await user.type(within(form).getByLabelText("Name"), "BV-2");
    expect(within(form).queryByText("Added BV-1.")).not.toBeInTheDocument();
  });

  it("drops the Can't add message once the problem is fixed", async () => {
    const user = userEvent.setup();
    renderTab();
    const form = await openAdd(user);
    await user.selectOptions(within(form).getByLabelText("Type"), "multi-state-value");
    await user.clear(within(form).getByLabelText("Name of state 1"));
    await user.type(within(form).getByLabelText("Name"), "MSV-1");
    await user.click(within(form).getByRole("button", { name: "Add object" }));
    expect(within(form).getByText("Can't add:")).toBeInTheDocument();
    await user.type(within(form).getByLabelText("Name of state 1"), "Off");
    expect(within(form).queryByText("Can't add:")).not.toBeInTheDocument();
  });

  it("won't add a value the new type can't use", async () => {
    const user = userEvent.setup();
    renderTab();
    const form = await openAdd(user);
    await user.selectOptions(within(form).getByLabelText("Type"), "analog-value");
    await user.type(within(form).getByLabelText("Relinquish default"), "72");
    await user.selectOptions(within(form).getByLabelText("Type"), "binary-value");
    await user.type(within(form).getByLabelText("Name"), "BV-1");
    await user.click(within(form).getByRole("button", { name: "Add object" }));
    expect(within(form).getByText("Can't add:").closest(".bp-error")).toHaveTextContent(
      "Can't add: Relinquish default 72 is not a binary value."
    );
    expect(objectsApi.create).not.toHaveBeenCalled();
  });

  it("won't add a schedule whose default doesn't fit its value type", async () => {
    const user = userEvent.setup();
    renderTab();
    const form = await openAdd(user);
    await user.selectOptions(within(form).getByLabelText("Type"), "schedule");
    await user.type(within(form).getByLabelText("Schedule default value"), "72");
    await user.selectOptions(within(form).getByLabelText("Schedule value type"), "binary");
    await user.type(within(form).getByLabelText("Name"), "SCH-1");
    await user.click(within(form).getByRole("button", { name: "Add object" }));
    expect(within(form).getByText("Can't add:").closest(".bp-error")).toHaveTextContent(
      "Can't add: Schedule default value 72 is not a binary value."
    );
    expect(objectsApi.create).not.toHaveBeenCalled();
  });

  it("won't add a multi-state object with a blank state name", async () => {
    const user = userEvent.setup();
    renderTab();
    const form = await openAdd(user);
    await user.selectOptions(within(form).getByLabelText("Type"), "multi-state-value");
    await user.clear(within(form).getByLabelText("Name of state 1"));
    await user.type(within(form).getByLabelText("Name"), "MSV-1");
    await user.click(within(form).getByRole("button", { name: "Add object" }));
    expect(within(form).getByText("Can't add:").closest(".bp-error")).toHaveTextContent("Can't add: State 1 needs a name.");
    expect(objectsApi.create).not.toHaveBeenCalled();
  });

  it("adds a calendar with its first date chosen from words", async () => {
    vi.mocked(objectsApi.create).mockResolvedValue({ ...a, id: 21, object_type: "calendar", name: "HOLIDAYS" } as never);
    const user = userEvent.setup();
    renderTab();
    const form = await openAdd(user);
    await user.selectOptions(within(form).getByLabelText("Type"), "calendar");
    await user.type(within(form).getByLabelText("Name"), "HOLIDAYS");
    await user.selectOptions(within(form).getByLabelText("Month"), "December");
    await user.selectOptions(within(form).getByLabelText("Day"), "25");
    await user.click(within(form).getByRole("button", { name: "Add object" }));
    expect(vi.mocked(objectsApi.create).mock.calls[0][1]).toMatchObject({
      date_list: [{ kind: "date", year: "*", month: "12", day: "25" }],
    });
  });

  it("adds a calendar with no dates when Start with a date is unticked", async () => {
    vi.mocked(objectsApi.create).mockResolvedValue({ ...a, id: 22, object_type: "calendar", name: "SPARE" } as never);
    const user = userEvent.setup();
    renderTab();
    const form = await openAdd(user);
    await user.selectOptions(within(form).getByLabelText("Type"), "calendar");
    await user.type(within(form).getByLabelText("Name"), "SPARE");
    await user.click(within(form).getByRole("checkbox", { name: "Start with a date" }));
    expect(within(form).queryByLabelText("Month")).not.toBeInTheDocument();
    await user.click(within(form).getByRole("button", { name: "Add object" }));
    expect(vi.mocked(objectsApi.create).mock.calls[0][1]).toMatchObject({ date_list: [] });
  });

  it("shows the trend fields only once Trended is ticked, and the interval only in interval mode", async () => {
    const user = userEvent.setup();
    renderTab();
    const form = await openAdd(user);
    expect(within(form).queryByLabelText("Trend mode")).not.toBeInTheDocument();
    await user.click(within(form).getByRole("checkbox", { name: "Trended" }));
    expect(within(form).getByLabelText("Interval (s)")).toBeInTheDocument();
    await user.selectOptions(within(form).getByLabelText("Trend mode"), "cov");
    expect(within(form).queryByLabelText("Interval (s)")).not.toBeInTheDocument();
  });

  it("offers no Trending fields for a notification class and never sends trended for it", async () => {
    vi.mocked(objectsApi.create).mockResolvedValue({ ...a, id: 21, name: "NC-1" } as never);
    const user = userEvent.setup();
    renderTab();
    const form = await openAdd(user);
    await user.click(within(form).getByRole("checkbox", { name: "Trended" }));
    await user.selectOptions(within(form).getByLabelText("Type"), "notification-class");
    expect(within(form).queryByRole("checkbox", { name: "Trended" })).not.toBeInTheDocument();
    await user.type(within(form).getByLabelText("Name"), "NC-1");
    await user.click(within(form).getByRole("button", { name: "Add object" }));
    expect(vi.mocked(objectsApi.create).mock.calls[0][1]).toMatchObject({ trended: false });
    expect(vi.mocked(objectsApi.create).mock.calls[0][1]).not.toHaveProperty("trend_mode");
  });

  it("puts an add error under the add button and keeps the form open", async () => {
    vi.mocked(objectsApi.create).mockRejectedValue(new Error("instance 1 already used"));
    const user = userEvent.setup();
    renderTab();
    const form = await openAdd(user);
    await user.type(within(form).getByLabelText("Name"), "AV-1");
    await user.click(within(form).getByRole("button", { name: "Add object" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent("Couldn't add the object: instance 1 already used");
    expect(within(form).getByLabelText("Name")).toHaveValue("AV-1");
  });

  it("says what BACnet writable covers on the chosen type", async () => {
    const user = userEvent.setup();
    renderTab();
    const form = await openAdd(user);
    await user.selectOptions(within(form).getByLabelText("Type"), "binary-value");
    expect(within(form).getByRole("checkbox", { name: "BACnet writable" })).toHaveAccessibleDescription(
      /can't command Present_Value\./
    );
    await user.selectOptions(within(form).getByLabelText("Type"), "calendar");
    expect(within(form).getByRole("checkbox", { name: "BACnet writable" })).toHaveAccessibleDescription(/configure this calendar/);
  });

  it("names every control in the add form", async () => {
    const user = userEvent.setup();
    renderTab();
    const form = await openAdd(user);
    await user.selectOptions(within(form).getByLabelText("Type"), "schedule");
    expectNamedControls(form);
  });
});

describe("Objects tab side panels", () => {
  beforeEach(() => {
    vi.mocked(objectsApi.list).mockResolvedValue([a, b] as never);
    vi.mocked(unitsApi.list).mockResolvedValue({ units: [] } as never);
  });

  it("opens Add object beside the list, which stays usable, and stays open after an add", async () => {
    const added = { ...base, id: 30, object_type: "binary-value", instance_number: 1, name: "BV-1" };
    vi.mocked(objectsApi.list).mockResolvedValueOnce([a, b] as never).mockResolvedValue([a, b, added] as never);
    vi.mocked(objectsApi.create).mockResolvedValue(added as never);
    const user = renderTab();
    await screen.findByLabelText("Select AV-A");
    await user.click(screen.getByRole("button", { name: "Add object" }));
    const panel = screen.getByRole("dialog", { name: "Add object" });
    expect(panel).toBeVisible();

    await user.click(screen.getByLabelText("Select AV-A"));
    expect(screen.getByLabelText("Select AV-A")).toBeChecked();

    const form = within(panel).getByRole("form", { name: "Add object" });
    await user.selectOptions(within(form).getByLabelText("Type"), "binary-value");
    await user.type(within(form).getByLabelText("Name"), "BV-1");
    await user.click(within(form).getByRole("button", { name: "Add object" }));
    expect(await within(form).findByText("Added BV-1.")).toBeInTheDocument();
    expect(await screen.findByText("BV-1", { selector: "td" })).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Add object" })).toBeVisible();
  });

  it("opens Edit selected for two ticked objects, titled with the count", async () => {
    const user = renderTab();
    await user.click(await screen.findByLabelText("Select AV-A"));
    await user.click(screen.getByLabelText("Select AV-B"));
    await user.click(screen.getByRole("button", { name: "Edit selected" }));
    expect(screen.getByRole("dialog", { name: "Edit 2 objects at once" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Apply to 2 objects" })).toBeEnabled();
  });

  it("replaces one panel with another and keeps the first one's draft", async () => {
    const user = renderTab();
    await user.click(await screen.findByLabelText("Select AV-A"));
    await user.click(screen.getByLabelText("Select AV-B"));
    await user.click(screen.getByRole("button", { name: "Edit selected" }));
    await user.click(screen.getByRole("checkbox", { name: "Set description" }));

    await user.click(screen.getByRole("button", { name: "Add object" }));
    expect(screen.getByRole("dialog", { name: "Add object" })).toBeVisible();
    expect(screen.queryByRole("dialog", { name: "Edit 2 objects at once" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Edit selected" }));
    expect(screen.queryByRole("dialog", { name: "Add object" })).not.toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Set description" })).toBeChecked();
  });

  it("opens the object's own overlay for Edit selected with one ticked", async () => {
    const user = renderTab();
    await user.click(await screen.findByLabelText("Select AV-A"));
    await user.click(screen.getByRole("button", { name: "Edit selected" }));
    expect(screen.getByTestId("where")).toHaveTextContent('/devices/1/objects/10 {"overlayFrom":"list"}');
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("follows a selection that drops to none: stays open, Apply disabled, focus to main on close", async () => {
    const user = renderTab();
    await user.click(await screen.findByLabelText("Select AV-A"));
    await user.click(screen.getByLabelText("Select AV-B"));
    await user.click(screen.getByRole("button", { name: "Edit selected" }));

    await user.click(screen.getByLabelText("Select AV-A"));
    expect(screen.getByRole("dialog", { name: "Edit 1 object" })).toBeVisible();
    await user.click(screen.getByLabelText("Select AV-B"));
    const panel = screen.getByRole("dialog", { name: "No objects selected" });
    expect(panel).toBeVisible();
    expect(within(panel).getByRole("button", { name: "Apply to 0 objects" })).toBeDisabled();

    await user.click(within(panel).getByRole("button", { name: "Close" }));
    expect(screen.getByRole("main")).toHaveFocus();
  });

  it("opens Simulate selected for two ticked objects", async () => {
    const user = renderTab();
    await user.click(await screen.findByLabelText("Select AV-A"));
    await user.click(screen.getByLabelText("Select AV-B"));
    await user.click(screen.getByRole("button", { name: "Simulate selected" }));
    expect(screen.getByRole("dialog", { name: "Simulate 2 objects" })).toBeVisible();
  });

  it("shows Add object above the list when there are no objects yet", async () => {
    vi.mocked(objectsApi.list).mockResolvedValue([] as never);
    const user = renderTab();
    await user.click(await screen.findByRole("button", { name: "Add object" }));
    expect(screen.getByRole("dialog", { name: "Add object" })).toBeVisible();
  });
});

describe("Objects tab panels: focus and fresh bulk forms", () => {
  beforeEach(() => {
    vi.mocked(objectsApi.list).mockResolvedValue([a, b] as never);
    vi.mocked(unitsApi.list).mockResolvedValue({ units: [] } as never);
  });

  async function tickBoth(user: ReturnType<typeof renderTab>) {
    await user.click(await screen.findByLabelText("Select AV-A"));
    await user.click(screen.getByLabelText("Select AV-B"));
  }

  it("keeps focus inside the panel that replaced another, and records the right opener", async () => {
    const user = renderTab();
    await tickBoth(user);
    await user.click(screen.getByRole("button", { name: "Add object" }));
    await user.click(screen.getByRole("button", { name: "Edit selected" }));
    expect(screen.getByRole("dialog", { name: "Edit 2 objects at once" })).toContainElement(document.activeElement as HTMLElement);

    await user.click(screen.getByRole("button", { name: "Add object" }));
    expect(screen.getByRole("dialog", { name: "Add object" })).toContainElement(document.activeElement as HTMLElement);
    await user.click(within(screen.getByRole("dialog", { name: "Add object" })).getByRole("button", { name: "Close" }));
    expect(screen.getAllByRole("button", { name: "Add object" })[0]).toHaveFocus();
  });

  it("starts the next Edit selected clean after a successful apply", async () => {
    vi.mocked(objectsApi.batchUpdate).mockResolvedValue([a, b] as never);
    const user = renderTab();
    await tickBoth(user);
    await user.click(screen.getByRole("button", { name: "Edit selected" }));
    await user.click(screen.getByRole("checkbox", { name: "Set description" }));
    await user.click(screen.getByRole("button", { name: "Apply to 2 objects" }));
    await vi.waitFor(() => expect(screen.queryByRole("dialog", { name: /Edit 2/ })).not.toBeInTheDocument());

    await tickBoth(user);
    await user.click(screen.getByRole("button", { name: "Edit selected" }));
    expect(screen.getByRole("checkbox", { name: "Set description" })).not.toBeChecked();
  });
});
