// The schedule editor as a tab panel: weekly entries, exceptions, write
// targets (local and remote, with their polled status) and what Save sends.
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ScheduleTab from "./ScheduleTab";
import { columnOf } from "../../testing/columns";
import { expectNamedControls } from "../../testing/controls";
import { clientToolApi, fetchAllObjectsWithDevice, objectsApi } from "../../api/endpoints";
import type { BacnetObjectDTO, ObjectWithDevice } from "../../api/types";

vi.mock("../../api/endpoints", () => ({
  objectsApi: { update: vi.fn().mockResolvedValue({}), get: vi.fn() },
  unitsApi: { list: vi.fn().mockResolvedValue({ units: [] }) },
  fetchAllObjectsWithDevice: vi.fn().mockResolvedValue([]),
  clientToolApi: { objectList: vi.fn() },
  engineApi: { status: vi.fn().mockResolvedValue([]) },
}));

function makeObj(overrides: Partial<BacnetObjectDTO> = {}): BacnetObjectDTO {
  return {
    id: 2,
    device_id: 1,
    object_type: "schedule",
    instance_number: 1,
    name: "SCH-1",
    description: null,
    units: null,
    bacnet_writable: true,
    out_of_service_default: false,
    relinquish_default: null,
    state_text: null,
    trended: false,
    trend_mode: "interval",
    trend_interval_seconds: null,
    trend_sample_cap: null,
    schedule_value_type: null,
    schedule_default: null,
    weekly_schedule: null,
    exception_schedule: null,
    schedule_effective_start: null,
    schedule_effective_end: null,
    schedule_write_targets: null,
    schedule_priority: 14,
    date_list: null,
    alarming_enabled: false,
    notification_class_instance: null,
    alarm_time_delay: 0,
    alarm_time_delay_normal: null,
    alarm_notify_type: "alarm",
    alarm_event_enable: null,
    alarm_high_limit: null,
    alarm_low_limit: null,
    alarm_deadband: 0,
    alarm_event_detection_enable: true,
    alarm_limit_enable: null,
    alarm_value: null,
    alarm_values: null,
    notification_priority: null,
    notification_ack_required: null,
    notification_recipient_list: null,
    ...overrides,
  } as BacnetObjectDTO;
}

function renderTab(obj: BacnetObjectDTO) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ScheduleTab deviceId={1} obj={obj} />
    </QueryClientProvider>
  );
}

describe("ScheduleTab", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders the schedule editor without a duplicate object heading", () => {
    renderTab(makeObj({ object_type: "schedule", schedule_value_type: "analog" }));
    // "weekly schedule" also appears inside the BACnet-writable checkbox's
    // label prose, so match the "Weekly schedule" fieldset by its legend
    // (exposed as the group's accessible name) rather than by raw text.
    expect(screen.getByRole("group", { name: "Weekly schedule" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 1 })).not.toBeInTheDocument();
  });

  const remoteTarget = {
    device_instance: 599005,
    object_type: "analog-value",
    instance_number: 3,
    property_identifier: "present-value",
  };

  it("shows a remote target's fields and its status", () => {
    renderTab(
      makeObj({
        schedule_value_type: "analog",
        schedule_default: "10.0",
        schedule_write_targets: [remoteTarget],
        schedule_write_status: {
          "599005:analog-value:3:present-value": {
            last_sent_at: null,
            last_sent_value: null,
            error: "device 599005 did not answer Who-Is",
            suppressed_by_dcc: false,
          },
        },
      })
    );
    expect(screen.getByLabelText("Device instance")).toHaveValue(599005);
    expect(screen.getByLabelText("Remote object type")).toHaveValue("analog-value");
    expect(screen.getByText("error: device 599005 did not answer Who-Is")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /browse remote device/i })).toBeInTheDocument();
  });

  it("offers a readable placeholder for a local target", () => {
    renderTab(
      makeObj({
        schedule_value_type: "analog",
        schedule_write_targets: [{ object_type: "analog-value", instance_number: 1, property_identifier: "presentValue" }],
      })
    );
    expect(screen.getByRole("option", { name: "Choose" })).toBeInTheDocument();
  });

  // A notification-class has no Present_Value to write.
  it("offers only local objects a schedule can write", async () => {
    vi.mocked(fetchAllObjectsWithDevice).mockResolvedValueOnce([
      { id: 10, device_id: 1, object_type: "analog-value", instance_number: 1, name: "AV-1" },
      { id: 11, device_id: 1, object_type: "notification-class", instance_number: 1, name: "ALARMS" },
    ] as ObjectWithDevice[]);
    renderTab(
      makeObj({
        schedule_value_type: "analog",
        schedule_write_targets: [{ object_type: "analog-value", instance_number: 1, property_identifier: "presentValue" }],
      })
    );
    expect(await screen.findByRole("option", { name: "AV-1 (analog-value:1)" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /ALARMS/ })).not.toBeInTheDocument();
  });

  it("shows 'not sent yet' for a remote target with no status", () => {
    renderTab(makeObj({ schedule_value_type: "analog", schedule_write_targets: [remoteTarget] }));
    expect(screen.getByText("not sent yet")).toBeInTheDocument();
  });

  it("adds a remote target and saves it", async () => {
    const user = userEvent.setup();
    renderTab(makeObj({ schedule_value_type: "analog", schedule_default: "10.0" }));
    await user.click(screen.getByRole("button", { name: /add remote target/i }));
    const device = screen.getByLabelText("Device instance");
    await user.clear(device);
    await user.type(device, "599005");
    const instance = screen.getByLabelText("Remote object instance");
    await user.clear(instance);
    await user.type(instance, "3");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(vi.mocked(objectsApi.update)).toHaveBeenCalledWith(
      1,
      2,
      expect.objectContaining({ schedule_write_targets: [remoteTarget] })
    );
  });
  it("a schedule with no default can still be edited and saved", async () => {
    renderTab(
      makeObj({
        schedule_value_type: "binary",
        schedule_default: null,
        weekly_schedule: [[{ time: "08:00", value: "active" }], [], [], [], [], [], []],
      })
    );
    const user = userEvent.setup();
    const field = await screen.findByLabelText("Default value");
    expect(field).toHaveValue("");
    expect(field).not.toBeRequired();
    expect(screen.getByRole("option", { name: "None (release targets)" })).toBeInTheDocument();
    const priority = screen.getByLabelText(/priority for writing/i);
    await user.clear(priority);
    await user.type(priority, "12");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(vi.mocked(objectsApi.update).mock.lastCall![2]).toEqual({ schedule_priority: 12 });
  });

  it("clearing the default sends null", async () => {
    renderTab(makeObj({ schedule_value_type: "binary", schedule_default: "inactive" }));
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByLabelText("Default value"), "");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(vi.mocked(objectsApi.update).mock.lastCall![2]).toEqual({ schedule_default: null });
  });

  // A BACnet client may have written the weekly schedule (or any
  // other schedule property) while the tab was open.
  it("sends only the fields edited here", async () => {
    renderTab(
      makeObj({
        schedule_value_type: "analog",
        schedule_default: "10.0",
        weekly_schedule: [[{ time: "08:00", value: "21.0" }], [], [], [], [], [], []],
      })
    );
    const user = userEvent.setup();
    const priority = await screen.findByLabelText(/priority for writing/i);
    await user.clear(priority);
    await user.type(priority, "12");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(vi.mocked(objectsApi.update).mock.lastCall![2]).toEqual({ schedule_priority: 12 });
  });

  it("sends every key a value-type change touches", async () => {
    renderTab(makeObj({ schedule_value_type: "analog", schedule_default: "1", units: "percent" }));
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByLabelText(/value type/i), "multi-state");
    await user.click(screen.getByRole("button", { name: "Off / On" }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(vi.mocked(objectsApi.update).mock.lastCall![2]).toEqual({
      schedule_value_type: "multi-state",
      units: null,
      state_text: ["Off", "On"],
    });
  });

  it("won't save with Priority for writing cleared", async () => {
    renderTab(makeObj({ schedule_value_type: "analog", schedule_default: "10.0" }));
    const user = userEvent.setup();
    await user.clear(await screen.findByLabelText(/priority for writing/i));
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(objectsApi.update).not.toHaveBeenCalled();
  });

  it("labels an exception's priority with 1 as the winner", () => {
    renderTab(
      makeObj({
        schedule_value_type: "analog",
        exception_schedule: [
          {
            period: { kind: "date", year: "*", month: "12", day: "25" },
            priority: 3,
            time_values: [],
          },
        ],
      })
    );
    expect(screen.getByLabelText("Priority (1 highest, 16 lowest)")).toHaveValue(3);
    expect(screen.queryByText(/higher wins/)).not.toBeInTheDocument();
  });

  const filledSchedule = {
    schedule_value_type: "analog",
    schedule_default: "70",
    weekly_schedule: [[{ time: "08:00", value: "72" }], [], [], [], [], [], []],
    exception_schedule: [
      {
        period: { kind: "date", year: "*", month: "12", day: "25", day_of_week: "*" },
        time_values: [{ time: "00:00", value: "60" }],
        priority: 16,
      },
    ],
    schedule_write_targets: [
      { object_type: "analog-value", instance_number: 1, property_identifier: "present-value" },
      { object_type: "analog-value", instance_number: 2, property_identifier: "present-value", device_instance: 599005 },
    ],
  } as Partial<BacnetObjectDTO>;
  it("echoes an exception time in device time and an effective date in ISO form", () => {
    renderTab(makeObj(filledSchedule));
    const time = screen.getByLabelText("Exception 1 time 1");
    fireEvent.change(time, { target: { value: "08:05" } });
    expect(within(time.closest(".bp-list-row") as HTMLElement).getByTestId("iso-echo")).toHaveTextContent(
      "08:05 device time"
    );
    const start = screen.getByLabelText("Start date");
    fireEvent.change(start, { target: { value: "2026-10-16" } });
    expect(within(start.closest(".bp-field") as HTMLElement).getByTestId("iso-echo")).toHaveTextContent(/^2026-10-16$/);
  });

  it("names every control in the schedule editor", async () => {
    const { container } = renderTab(makeObj(filledSchedule));
    await screen.findByRole("group", { name: "Weekly schedule" });
    expectNamedControls(container);
    expect(screen.getByLabelText("Value, entry 1")).toHaveValue(72);
    expect(screen.getByRole("button", { name: "Remove Exception 1 time 1" })).toBeInTheDocument();
  });

  it("puts a save error under Save", async () => {
    vi.mocked(objectsApi.update).mockRejectedValueOnce(new Error("priority out of range"));
    const user = userEvent.setup();
    renderTab(makeObj(filledSchedule));
    await user.click(await screen.findByRole("button", { name: "Save" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Couldn't save: priority out of range");
    expect(screen.getByRole("button", { name: "Save" }).closest("form")).toContainElement(alert);
  });

  it("counts an edit as saved once the server confirms it, before any refetch", async () => {
    vi.mocked(objectsApi.update).mockResolvedValueOnce({} as never);
    const user = userEvent.setup();
    renderTab(makeObj(filledSchedule));
    await user.type(await screen.findByLabelText("Default value"), "1");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Saved.");
  });

  it("keeps an unsaved schedule edit when another tab's save refreshes the object", async () => {
    const user = userEvent.setup();
    const client = new QueryClient();
    const tab = (o: BacnetObjectDTO) => (
      <QueryClientProvider client={client}>
        <ScheduleTab deviceId={599002} obj={o} />
      </QueryClientProvider>
    );
    const { rerender } = render(tab(makeObj({ schedule_value_type: "analog", schedule_default: "70" } as Partial<BacnetObjectDTO>)));
    await user.type(await screen.findByLabelText("Default value"), "1");
    rerender(tab(makeObj({ schedule_value_type: "analog", schedule_default: "70", schedule_priority: 9 } as Partial<BacnetObjectDTO>)));
    expect(screen.getByLabelText("Priority for writing (1-16)")).toHaveValue(9);
    expect(screen.getByLabelText("Default value")).toHaveValue(701);
  });

  it("draws a client's NULL entry as the default and doesn't resend the week", async () => {
    const user = userEvent.setup();
    const monday = [
      { time: "08:00", value: "active" },
      { time: "17:00", value: null },
    ];
    renderTab(
      makeObj({
        schedule_value_type: "binary",
        schedule_default: "inactive",
        weekly_schedule: [monday, [], [], [], [], [], []],
      } as Partial<BacnetObjectDTO>)
    );
    const blocks = document.querySelectorAll(".bp-sched__row")[0].querySelectorAll(".bp-sched__seg");
    expect(blocks).toHaveLength(1);
    expect(screen.getByLabelText("Value, entry 2")).toHaveDisplayValue("Default (Inactive)");
    // user.clear doesn't empty a number input in jsdom.
    fireEvent.change(screen.getByLabelText("Priority for writing (1-16)"), { target: { value: "9" } });
    await user.click(screen.getByRole("button", { name: "Save" }));
    const patch = vi.mocked(objectsApi.update).mock.calls[0][2];
    expect(patch).toEqual({ schedule_priority: 9 });
  });

  it("offers a multi-state schedule's values by its own state names", () => {
    renderTab(
      makeObj({
        schedule_value_type: "multi-state",
        schedule_default: "3",
        state_text: ["Occupied", "Standby", "Unoccupied"],
        weekly_schedule: [[{ time: "06:00", value: "1" }], [], [], [], [], [], []],
      } as Partial<BacnetObjectDTO>)
    );
    expect(screen.getByLabelText("Default value")).toHaveDisplayValue("3 · Unoccupied");
    expect(screen.getByLabelText("Value, entry 1")).toHaveDisplayValue("1 · Occupied");
    expect(screen.getByText("06:00 Occupied")).toBeInTheDocument();
  });

  it("keeps the schedule's own values on their states when the states move", async () => {
    const user = userEvent.setup();
    renderTab(
      makeObj({
        schedule_value_type: "multi-state",
        schedule_default: "3",
        state_text: ["Occupied", "Standby", "Unoccupied"],
        weekly_schedule: [[{ time: "06:00", value: "1" }], [], [], [], [], [], []],
      } as Partial<BacnetObjectDTO>)
    );
    await user.click(screen.getByRole("button", { name: "Move state 1 down" }));
    expect(screen.getByLabelText("Value, entry 1")).toHaveDisplayValue("2 · Occupied");
    expect(screen.getByLabelText("Default value")).toHaveDisplayValue("3 · Unoccupied");
  });

  it("won't save a day with two entries at one time, and names them", async () => {
    const user = userEvent.setup();
    renderTab(
      makeObj({
        schedule_value_type: "binary",
        schedule_default: "inactive",
        weekly_schedule: [
          [
            { time: "08:00", value: "active" },
            { time: "08:00", value: "inactive" },
          ],
          [],
          [],
          [],
          [],
          [],
          [],
        ],
      } as Partial<BacnetObjectDTO>)
    );
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(screen.getByText("Can't save:").closest(".bp-error")).toHaveTextContent("Can't save: Two Monday entries are at 08:00.");
    expect(objectsApi.update).not.toHaveBeenCalled();
  });

  it("says a multi-state schedule saved without state names needs them, and how many", () => {
    renderTab(
      makeObj({
        schedule_value_type: "multi-state",
        schedule_default: "1",
        state_text: null,
        weekly_schedule: [[{ time: "08:00", value: "3" }], [], [], [], [], [], []],
      } as Partial<BacnetObjectDTO>)
    );
    expect(screen.getByText(/has no state names/).closest(".bp-callout")).toHaveTextContent(
      "This schedule has no state names. Its values use states 1 to 3. Name them below to save it."
    );
  });

  it("won't save values the new value type can't use", async () => {
    const user = userEvent.setup();
    renderTab(
      makeObj({
        schedule_value_type: "analog",
        schedule_default: "72",
        weekly_schedule: [[{ time: "08:00", value: "68" }], [], [], [], [], [], []],
      } as Partial<BacnetObjectDTO>)
    );
    await user.selectOptions(await screen.findByLabelText(/value type/i), "binary");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(screen.getByText("Can't save:").closest(".bp-error")).toHaveTextContent(
      "Can't save: Default value 72 is not a binary value."
    );
    await user.selectOptions(screen.getByLabelText("Default value"), "Inactive");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(screen.getByText("Can't save:").closest(".bp-error")).toHaveTextContent(
      "Can't save: Monday 08:00: 68 is not a binary value."
    );
    expect(objectsApi.update).not.toHaveBeenCalled();
  });

  it("drops the Can't save message once the problem is fixed", async () => {
    const user = userEvent.setup();
    renderTab(makeObj({ schedule_value_type: "binary", schedule_default: "72" } as Partial<BacnetObjectDTO>));
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(screen.getByText("Can't save:")).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Default value"), "Inactive");
    expect(screen.queryByText("Can't save:")).not.toBeInTheDocument();
  });

  it("flags an exception entry that repeats a time on its row", () => {
    renderTab(
      makeObj({
        schedule_value_type: "binary",
        schedule_default: "inactive",
        exception_schedule: [
          {
            period: { kind: "date", year: "*", month: "12", day: "25" },
            priority: 16,
            time_values: [
              { time: "08:00", value: "active" },
              { time: "08:00", value: "inactive" },
            ],
          },
        ],
      } as Partial<BacnetObjectDTO>)
    );
    expect(screen.getByLabelText("Exception 1 time 2")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText("Exception 1 time 1")).not.toHaveAttribute("aria-invalid");
    expect(screen.getByText("Entry 2 repeats 08:00.")).toBeInTheDocument();
  });

  it("won't save an exception entry with its time cleared", async () => {
    const user = userEvent.setup();
    renderTab(makeObj({ schedule_value_type: "binary", schedule_default: "inactive" } as Partial<BacnetObjectDTO>));
    await user.click(screen.getByRole("button", { name: "+ Add exception" }));
    await user.click(screen.getByRole("button", { name: "+ Add time" }));
    fireEvent.change(screen.getByLabelText("Exception 1 time 1"), { target: { value: "" } });
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(screen.getByText("Can't save:").closest(".bp-error")).toHaveTextContent(
      "Can't save: Exception 1 entry 1 has no time."
    );
    expect(objectsApi.update).not.toHaveBeenCalled();
  });

  it("offers End exception as an exception entry's empty value", async () => {
    const user = userEvent.setup();
    renderTab(makeObj({ schedule_value_type: "binary", schedule_default: "inactive" } as Partial<BacnetObjectDTO>));
    await user.click(screen.getByRole("button", { name: "+ Add exception" }));
    await user.click(screen.getByRole("button", { name: "+ Add time" }));
    const value = screen.getByLabelText("Exception 1 value 1");
    expect(value).toHaveDisplayValue("Active");
    await user.selectOptions(value, "End exception");
    expect(value).toHaveValue("");
    expect(screen.getByLabelText("Priority (1 highest, 16 lowest)")).toBeInTheDocument();
  });
});

describe("remote write targets", () => {
  const remote = { device_instance: 599005, object_type: "binary-value", instance_number: 3, property_identifier: "present-value" };
  const key = "599005:binary-value:3:present-value";

  beforeEach(() => {
    // Earlier tests save (objectsApi.update); the poll needs an answer.
    vi.clearAllMocks();
    vi.mocked(objectsApi.get).mockResolvedValue(makeObj());
  });

  it("shows the polled status without resetting an edited field", async () => {
    // The status comes from its own query (objectsApi.get), not the obj
    // prop, so its arrival can't reset the form. No fake timers: the first
    // fetch is immediate, and refetchInterval is TanStack's, not ours.
    const obj = makeObj({ schedule_value_type: "binary", schedule_write_targets: [remote] });
    let release: () => void = () => {};
    vi.mocked(objectsApi.get).mockReturnValue(
      new Promise((resolve) => {
        release = () =>
          resolve({
            ...obj,
            schedule_write_status: {
              [key]: { last_sent_at: "2026-10-01T12:00:00+00:00", last_sent_value: 1, error: null, suppressed_by_dcc: false },
            },
          });
      })
    );
    renderTab(obj);
    const user = userEvent.setup();
    const instance = await screen.findByLabelText("Remote object instance");
    await user.clear(instance);
    await user.type(instance, "7");

    release();
    await vi.waitFor(() => expect(objectsApi.get).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 0)); // let the resolved status render
    expect(screen.getByLabelText("Remote object instance")).toHaveValue(7);
  });

  it("shows a binary remote target's polled status as active", async () => {
    const obj = makeObj({ schedule_value_type: "binary", schedule_write_targets: [remote] });
    vi.mocked(objectsApi.get).mockResolvedValue({
      ...obj,
      schedule_write_status: {
        [key]: { last_sent_at: "2026-10-01T12:00:00+00:00", last_sent_value: 1, error: null, suppressed_by_dcc: false },
      },
    });
    renderTab(obj);
    expect(await screen.findByText(/sent active at 2026-10-01 12:00:00 UTC/)).toBeInTheDocument();
  });

  it("shows a status the poll cleared as cleared, not the status the object was loaded with", async () => {
    const obj = makeObj({
      schedule_value_type: "binary",
      schedule_write_targets: [remote],
      schedule_write_status: {
        [key]: { last_sent_at: "2026-10-01T12:00:00+00:00", last_sent_value: 1, error: null, suppressed_by_dcc: false },
      },
    });
    vi.mocked(objectsApi.get).mockResolvedValue({ ...obj, schedule_write_status: null });
    renderTab(obj);
    expect(await screen.findByText(/not sent yet/)).toBeInTheDocument();
    expect(screen.queryByText(/sent active/)).not.toBeInTheDocument();
  });

  it("won't browse a remote row with no device", async () => {
    renderTab(makeObj({ schedule_value_type: "analog" }));
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "+ Add remote target" }));
    expect(screen.getByRole("button", { name: "Browse remote device" })).toBeDisabled();
    await user.type(screen.getByLabelText("Device instance"), "599005");
    expect(screen.getByRole("button", { name: "Browse remote device" })).toBeEnabled();
  });

  it("starts a new remote row from the previous row's device", async () => {
    renderTab(makeObj({ schedule_value_type: "analog", schedule_write_targets: [{ ...remote, object_type: "analog-value" }] }));
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "+ Add remote target" }));
    const devices = screen.getAllByLabelText("Device instance");
    expect(devices[1]).toHaveValue(599005);
  });

  it("won't save a remote row with no device", async () => {
    // A default, so the browser's own required-field check lets the form submit.
    renderTab(makeObj({ schedule_value_type: "analog", schedule_default: "10.0" }));
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "+ Add remote target" }));
    expect(screen.getByLabelText("Device instance")).toHaveValue(null);
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(objectsApi.update).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(/device instance/i);
  });

  it("removing a row doesn't hand its browsed list to the next row", async () => {
    vi.mocked(clientToolApi.objectList).mockResolvedValue([
      { object_type: "binary-value", instance: 3, name: "Fan" },
    ]);
    renderTab(makeObj({ schedule_value_type: "binary", schedule_write_targets: [remote, { ...remote, instance_number: 4 }] }));
    const user = userEvent.setup();
    const [firstBrowse] = await screen.findAllByRole("button", { name: "Browse remote device" });
    await user.click(firstBrowse);
    expect(await screen.findAllByLabelText("Remote object")).toHaveLength(1);

    await user.click(screen.getAllByRole("button", { name: "Remove" })[0]);

    expect(screen.getAllByLabelText("Remote object instance")).toHaveLength(1);
    expect(screen.getByLabelText("Remote object instance")).toHaveValue(4);
    expect(screen.queryByLabelText("Remote object")).not.toBeInTheDocument();
  });
});

describe("ScheduleTab on a wide screen", () => {
  it("lays Exception schedule and Write targets side by side under the full-width weekly schedule", () => {
    renderTab(makeObj({ schedule_value_type: "analog", schedule_default: "0" }));
    const legend = (name: string) => screen.getByText(name, { selector: "legend" });
    expect(columnOf(legend("Exception schedule"))).toBe(0);
    expect(columnOf(legend("Write targets"))).toBe(1);
    expect(columnOf(legend("Weekly schedule"))).toBeNull();
    for (const name of ["Value", "BACnet access", "Effective period"]) {
      expect(legend(name).closest(".bp-schedule-top")).not.toBeNull();
    }
  });
});
