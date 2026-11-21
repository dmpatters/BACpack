import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import GeneralTab from "./GeneralTab";
import { expectNamedControls } from "../../testing/controls";
import { columnOf } from "../../testing/columns";
import { objectsApi } from "../../api/endpoints";
import type { BacnetObjectDTO } from "../../api/types";

vi.mock("../../api/endpoints", () => ({
  objectsApi: { update: vi.fn() },
  unitsApi: { list: vi.fn().mockResolvedValue({ units: ["degreesFahrenheit", "percent"] }) },
}));

function makeObj(overrides: Partial<BacnetObjectDTO> = {}): BacnetObjectDTO {
  return {
    id: 5,
    device_id: 599002,
    object_type: "analog-input",
    instance_number: 1,
    name: "AI-1",
    description: "Simulated analog input",
    units: "degreesFahrenheit",
    bacnet_writable: true,
    out_of_service_default: false,
    relinquish_default: null,
    state_text: null,
    trended: false,
    trend_mode: "interval",
    ...overrides,
  } as BacnetObjectDTO;
}

function renderTab(obj: BacnetObjectDTO) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <GeneralTab deviceId={599002} obj={obj} />
    </QueryClientProvider>
  );
}

describe("GeneralTab", () => {
  it("shows the fields that used to be edited in the grid", () => {
    renderTab(makeObj({ object_type: "analog-output" }));
    expect(screen.getByLabelText(/name/i)).toHaveValue("AI-1");
    expect(screen.getByLabelText(/description/i)).toHaveValue("Simulated analog input");
    expect(screen.getByRole("combobox", { name: "Units" })).toBeInTheDocument();
    expect(screen.getByLabelText(/relinquish default/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/out of service/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/bacnet writable/i)).toBeInTheDocument();
  });

  it("stacks Behavior under Object in one form column, with one Save under both", () => {
    renderTab(makeObj());
    const object = screen.getByRole("group", { name: "Object" });
    const behavior = screen.getByRole("group", { name: "Behavior" });
    expect(columnOf(object)).toBeNull();
    expect(columnOf(behavior)).toBeNull();
    expect(object.closest(".bp-columns")).toBeNull();
    expect(object.compareDocumentPosition(behavior) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(object.closest("form")).toBe(behavior.closest("form"));
    expect(object.closest("form")).not.toHaveClass("bp-form--wide");
  });

  it("says what BACnet writable covers on this type", () => {
    renderTab(makeObj({ object_type: "analog-value" }));
    expect(screen.getByRole("checkbox", { name: "BACnet writable" })).toHaveAccessibleDescription(
      /can't command Present_Value\./
    );
  });

  // The API ignores Units and Relinquish default for a Schedule.
  it("hides relinquish default for a type with no priority array", () => {
    renderTab(makeObj({ object_type: "analog-input" }));
    expect(screen.getByRole("combobox", { name: "Units" })).toBeInTheDocument();
    expect(screen.queryByLabelText(/relinquish default/i)).not.toBeInTheDocument();
  });

  it("shows relinquish default for a multi-state value (0.28.0: commandable)", () => {
    renderTab(makeObj({ object_type: "multi-state-value", state_text: ["Off", "Low", "High"] }));
    expect(screen.getByLabelText(/relinquish default/i)).toBeInTheDocument();
  });

  it("hides units and relinquish default on a binary schedule", () => {
    renderTab(makeObj({ object_type: "schedule", schedule_value_type: "binary" } as Partial<BacnetObjectDTO>));
    expect(screen.queryByRole("combobox", { name: "Units" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/relinquish default/i)).not.toBeInTheDocument();
  });

  it("keeps units on an analog schedule", () => {
    renderTab(makeObj({ object_type: "schedule", schedule_value_type: "analog" } as Partial<BacnetObjectDTO>));
    expect(screen.getByRole("combobox", { name: "Units" })).toBeInTheDocument();
  });

  it("hides the state-text fields for a type that has no states", () => {
    renderTab(makeObj({ object_type: "analog-input" }));
    expect(screen.queryByLabelText(/state text/i)).not.toBeInTheDocument();
  });

  it("shows the state-text fields for a multi-state type", () => {
    renderTab(makeObj({ object_type: "multi-state-value", state_text: ["Off", "Low", "High"] }));
    expect(screen.getByRole("list", { name: "State names" })).toBeInTheDocument();
    expect(screen.getByLabelText("Name of state 3")).toHaveValue("High");
  });

  it("saves edited fields through objectsApi.update", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj({ name: "AI-1-renamed" }));
    renderTab(makeObj());

    await user.clear(screen.getByLabelText(/name/i));
    await user.type(screen.getByLabelText(/name/i), "AI-1-renamed");
    await user.click(screen.getByRole("button", { name: /save/i }));

    expect(objectsApi.update).toHaveBeenCalledWith(
      599002,
      5,
      expect.objectContaining({ name: "AI-1-renamed" })
    );
  });

  it("says a running device applies the save", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(makeObj());

    await user.click(screen.getByRole("button", { name: /save/i }));

    expect(await screen.findByText("Saved.")).toBeInTheDocument();
  });

  // Like the Alarming tab, a save here
  // must not put back reporting settings a BACnet client wrote meanwhile.
  it("does not resend the trend reporting fields when they weren't changed", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(
      makeObj({
        trended: true,
        trend_reporting_enabled: true,
        trend_notification_threshold: 5,
        trend_notification_class_instance: 1,
        trend_event_detection_enable: true,
      } as Partial<BacnetObjectDTO>)
    );
    await user.clear(screen.getByLabelText(/name/i));
    await user.type(screen.getByLabelText(/name/i), "AI-1-renamed");
    await user.click(screen.getByRole("button", { name: /save/i }));
    const patch = vi.mocked(objectsApi.update).mock.lastCall![2];
    for (const key of [
      "trend_reporting_enabled",
      "trend_notification_threshold",
      "trend_notification_class_instance",
      "trend_event_detection_enable",
    ]) {
      expect(patch).not.toHaveProperty(key);
    }
  });
  // A BACnet client may have written State_Text (or the name,
  // description, relinquish default) while the tab was open.
  it("sends only the fields edited here", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(
      makeObj({ object_type: "multi-state-value", state_text: ["Off", "Low", "High"], relinquish_default: "1" })
    );
    await user.type(screen.getByLabelText(/description/i), " (moved)");
    await user.click(screen.getByRole("button", { name: /save/i }));
    expect(vi.mocked(objectsApi.update).mock.lastCall![2]).toEqual({
      description: "Simulated analog input (moved)",
    });
  });
  it("hides 'Saved' again once the form is edited after a save", async () => {
    vi.mocked(objectsApi.update).mockResolvedValue({} as never);
    const user = userEvent.setup();
    renderTab(makeObj({ object_type: "analog-value" }));
    await user.type(screen.getByLabelText("Description"), " x");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Saved");
    await user.type(screen.getByLabelText("Description"), "y");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("puts a save error under Save", async () => {
    vi.mocked(objectsApi.update).mockRejectedValue(new Error("name already used"));
    const user = userEvent.setup();
    renderTab(makeObj());
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't save: name already used");
  });

  it("names every control", () => {
    const { container } = renderTab(makeObj({ object_type: "multi-state-value", units: null, state_text: ["Off", "On"] }));
    expectNamedControls(container);
  });

  it("keeps an unsaved edit when another tab's save refreshes the object", async () => {
    const user = userEvent.setup();
    const client = new QueryClient();
    const tab = (o: BacnetObjectDTO) => (
      <QueryClientProvider client={client}>
        <GeneralTab deviceId={599002} obj={o} />
      </QueryClientProvider>
    );
    const { rerender } = render(tab(makeObj({ object_type: "analog-value" })));
    await user.type(screen.getByLabelText("Description"), " edited");
    rerender(tab(makeObj({ object_type: "analog-value", name: "AI-renamed" })));
    expect(screen.getByLabelText("Name")).toHaveValue("AI-renamed");
    expect(screen.getByLabelText("Description")).toHaveValue("Simulated analog input edited");
  });
});

describe("GeneralTab State_Text label with a comma, and units", () => {
  it("saves a label holding a comma as one state", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(makeObj({ object_type: "multi-state-value", units: null, state_text: ["Low, fan off", "High"] }));

    await user.tripleClick(screen.getByLabelText("Name of state 2"));
    await user.keyboard("High speed");
    await user.click(screen.getByRole("button", { name: /save/i }));

    expect(vi.mocked(objectsApi.update).mock.lastCall![2]).toEqual({ state_text: ["Low, fan off", "High speed"] });
  });

  it("picks units from the grouped list and saves the enum", async () => {
    vi.mocked(objectsApi.update).mockResolvedValue({} as never);
    const user = userEvent.setup();
    renderTab(makeObj({ object_type: "analog-value", units: "degreesFahrenheit" }));
    const units = await screen.findByDisplayValue("Degrees Fahrenheit (°F)");
    await user.clear(units);
    await user.type(units, "percent (");
    await user.keyboard("{Enter}");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(objectsApi.update).toHaveBeenCalledWith(599002, 5, expect.objectContaining({ units: "percent" }));
  });

  it("keeps an imported unit the list doesn't have when saving something else", async () => {
    vi.mocked(objectsApi.update).mockResolvedValue({} as never);
    const user = userEvent.setup();
    renderTab(makeObj({ object_type: "analog-value", units: "degF" }));
    expect(await screen.findByDisplayValue("degF (not a BACnet unit)")).toHaveRole("combobox");
    await user.type(screen.getByLabelText("Description"), " x");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(vi.mocked(objectsApi.update).mock.calls[0][2]).not.toHaveProperty("units");
  });
});

describe("GeneralTab states", () => {
  beforeEach(() => vi.clearAllMocks());

  const msv = (overrides: Partial<BacnetObjectDTO> = {}) =>
    makeObj({ object_type: "multi-state-value", units: null, state_text: ["Off", "Low", "High"], relinquish_default: "1", ...overrides });

  it("edits the states in one list and saves them", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockResolvedValue(msv());
    renderTab(msv());
    await user.click(screen.getByRole("button", { name: "Add state" }));
    await user.clear(screen.getByLabelText("Name of state 4"));
    await user.type(screen.getByLabelText("Name of state 4"), "Auto");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(vi.mocked(objectsApi.update).mock.calls[0][2]).toEqual({ state_text: ["Off", "Low", "High", "Auto"] });
  });

  it("won't save names a client wrote blank or repeated, and says why", async () => {
    const user = userEvent.setup();
    renderTab(msv({ state_text: ["Off", "", "Off"], relinquish_default: null }));
    expect(screen.getByLabelText("Name of state 2")).toHaveAccessibleDescription("State 2 needs a name.");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(screen.getByText("Can't save:").closest(".bp-error")).toHaveTextContent("Can't save: State 2 needs a name.");
    expect(objectsApi.update).not.toHaveBeenCalled();
  });

  it("drops the Can't save message once the problem is fixed", async () => {
    const user = userEvent.setup();
    renderTab(msv({ state_text: ["Off", "", "High"], relinquish_default: null }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(screen.getByText("Can't save:")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Name of state 2"), "Low");
    expect(screen.queryByText("Can't save:")).not.toBeInTheDocument();
  });

  it("picks the relinquish default by name and keeps it on its state through a move", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockResolvedValue(msv());
    renderTab(msv());
    const relinquish = screen.getByLabelText("Relinquish default");
    expect(relinquish).toHaveDisplayValue("1 · Off");
    await user.click(screen.getByRole("button", { name: "Move state 1 down" }));
    expect(relinquish).toHaveDisplayValue("2 · Off");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(vi.mocked(objectsApi.update).mock.calls[0][2]).toEqual({
      state_text: ["Low", "Off", "High"],
      relinquish_default: "2",
    });
  });

  it("clears the relinquish default when its state is deleted", async () => {
    const user = userEvent.setup();
    renderTab(msv());
    await user.click(screen.getByRole("button", { name: "Delete state 1" }));
    expect(screen.getByLabelText("Relinquish default")).toHaveDisplayValue("None");
  });

  it("offers a binary relinquish default as words", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(makeObj({ object_type: "binary-value", units: null, relinquish_default: "active" }));
    const relinquish = screen.getByLabelText("Relinquish default");
    expect(relinquish).toHaveDisplayValue("Active");
    await user.selectOptions(relinquish, "None");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(vi.mocked(objectsApi.update).mock.calls[0][2]).toEqual({ relinquish_default: null });
  });

  it("warns that a schedule on this device uses the states", () => {
    const schedule = makeObj({
      id: 9,
      object_type: "schedule",
      name: "OCC_SCHEDULE",
      schedule_value_type: "multi-state",
      schedule_default: "3",
      weekly_schedule: [[], [], [], [], [], [], []],
      exception_schedule: [],
      schedule_write_targets: [{ object_type: "multi-state-value", instance_number: 1, property_identifier: "present-value" }],
    });
    const obj = msv({ instance_number: 1 });
    render(
      <QueryClientProvider client={new QueryClient()}>
        <GeneralTab deviceId={599002} obj={obj} objects={[obj, schedule]} />
      </QueryClientProvider>
    );
    expect(screen.getByText(/Schedule OCC_SCHEDULE uses state 3 \(High\)\./)).toBeInTheDocument();
  });

  it("shows Active text and Inactive text on a binary object, defaulting to On and Off", () => {
    renderTab(makeObj({ object_type: "binary-value", units: null, active_text: "Open" }));
    expect(screen.getByLabelText("Active text")).toHaveValue("Open");
    expect(screen.getByLabelText("Inactive text")).toHaveValue("");
    expect(screen.getByLabelText("Inactive text")).toHaveAttribute("placeholder", "Off");
  });

  it("has no Active text on a non-binary object", () => {
    renderTab(makeObj({ object_type: "analog-value" }));
    expect(screen.queryByLabelText("Active text")).not.toBeInTheDocument();
  });

  it("shows Start value on an input and not on an output", () => {
    const { unmount } = renderTab(makeObj({ object_type: "analog-input", start_value: "21.5" }));
    expect(screen.getByLabelText(/start value/i)).toHaveValue(21.5);
    unmount();
    renderTab(makeObj({ object_type: "analog-output" }));
    expect(screen.queryByLabelText(/start value/i)).not.toBeInTheDocument();
  });

  it("saves an edited Active text and Start value", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(makeObj({ object_type: "binary-input", units: null }));
    await user.type(screen.getByLabelText("Active text"), "Alarm");
    await user.selectOptions(screen.getByLabelText(/start value/i), "active");
    await user.click(screen.getByRole("button", { name: /save/i }));
    expect(vi.mocked(objectsApi.update).mock.lastCall![2]).toEqual({ active_text: "Alarm", start_value: "active" });
  });
});
