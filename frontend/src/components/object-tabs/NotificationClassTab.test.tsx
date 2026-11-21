// Covers RecipientListEditor, the notification-class recipient-list editor
// (its pure rendering/change logic, matching AlarmingTab.test.tsx's
// AlarmFieldsEditor precedent), and the connected NotificationClassTab with
// objectsApi mocked, such as a rejected save. The rest of the tab is API-driven
// and covered at the backend/API level
// (tests/objects/test_notification_class.py) plus manual verification.
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import { RecipientListEditor, emptyRecipient, recipientSummary } from "./NotificationClassTab";
import NotificationClassTab from "./NotificationClassTab";
import { expectNamedControls } from "../../testing/controls";
import type { BacnetObjectDTO, NotificationRecipientEntry } from "../../api/types";
import { objectsApi } from "../../api/endpoints";

vi.mock("../../api/endpoints", () => ({
  objectsApi: {
    update: vi.fn(),
  },
}));

describe("RecipientListEditor", () => {
  it("renders one row per recipient", () => {
    const recipients: NotificationRecipientEntry[] = [emptyRecipient(), emptyRecipient()];
    render(<RecipientListEditor recipients={recipients} onChange={() => {}} />);
    expect(screen.getAllByRole("button", { name: "Delete" })).toHaveLength(2);
  });

  it("calls onChange with an added row when 'Add recipient' is clicked", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<RecipientListEditor recipients={[]} onChange={onChange} />);

    await user.click(screen.getByRole("button", { name: "Add recipient" }));

    expect(onChange).toHaveBeenCalledWith([emptyRecipient()]);
  });

  it("calls onChange with the row removed when its Delete button is clicked", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const recipients: NotificationRecipientEntry[] = [emptyRecipient(), emptyRecipient()];
    render(<RecipientListEditor recipients={recipients} onChange={onChange} />);

    await user.click(screen.getAllByRole("button", { name: "Delete" })[0]);

    expect(onChange).toHaveBeenCalledWith([recipients[1]]);
  });

  it("switches a row from address to device-instance kind", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const recipients: NotificationRecipientEntry[] = [emptyRecipient()];
    render(<RecipientListEditor recipients={recipients} onChange={onChange} />);

    await user.selectOptions(screen.getByRole("combobox"), "device");

    expect(onChange).toHaveBeenCalledWith([
      { ...recipients[0], recipient: { kind: "device", instance_number: 0 } },
    ]);
  });
});

// --- NotificationClassTab as a tab panel: driven by props, not a route ---

function makeObj(overrides: Partial<BacnetObjectDTO> = {}): BacnetObjectDTO {
  return {
    id: 2,
    device_id: 1,
    object_type: "notification-class",
    instance_number: 1,
    name: "NC-1",
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
      <NotificationClassTab deviceId={1} obj={obj} />
    </QueryClientProvider>
  );
}

describe("NotificationClassTab", () => {
  // A rejected save must show the user why, not just silently fail to navigate.
  it("shows the server's error message when the save is rejected", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockRejectedValue(
      new Error("a device recipient requires instance_number")
    );

    renderTab(makeObj());

    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(
        screen.getByText("a device recipient requires instance_number")
      ).toBeInTheDocument();
    });
  });

  it("stays on the tab after a successful save", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(makeObj({ object_type: "notification-class" }));
    await user.click(screen.getByRole("button", { name: /save/i }));
    expect(await screen.findByText(/saved/i)).toBeInTheDocument();
  });

  it("says a running device applies the save", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(makeObj({ object_type: "notification-class" }));
    await user.click(screen.getByRole("button", { name: /save/i }));
    expect(await screen.findByText("Saved.")).toBeInTheDocument();
  });
  // A client may have added a recipient with AddListElement while
  // the tab was open; priority and ack_required aren't editable here.
  it("sends nothing it didn't change", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(
      makeObj({
        object_type: "notification-class",
        notification_priority: [1, 2, 3],
        notification_ack_required: [true, false, true],
        notification_recipient_list: [emptyRecipient()],
      })
    );
    await user.click(screen.getByRole("button", { name: /save/i }));
    expect(vi.mocked(objectsApi.update).mock.lastCall![2]).toEqual({});
  });

  it("sends the recipient list when it changed", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(makeObj({ object_type: "notification-class", notification_recipient_list: [] }));
    await user.click(screen.getByRole("button", { name: /add recipient/i }));
    await user.click(screen.getByRole("button", { name: /save/i }));
    expect(vi.mocked(objectsApi.update).mock.lastCall![2]).toEqual({
      notification_recipient_list: [emptyRecipient()],
    });
  });
  // If the tab kept its edited list after a save, a recipient a client
  // added (seen after a refetch) would be dropped by the next save.
  it("shows the saved list after a save, so a later save keeps a client's recipient", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wire = { ...emptyRecipient(), process_identifier: 9 };
    const { rerender } = render(
      <QueryClientProvider client={client}>
        <NotificationClassTab deviceId={1} obj={makeObj({ object_type: "notification-class", notification_recipient_list: [] })} />
      </QueryClientProvider>
    );
    await user.click(screen.getByRole("button", { name: /add recipient/i }));
    await user.click(screen.getByRole("button", { name: /save/i }));
    await screen.findByText(/saved/i);
    // The refetch brings the saved recipient plus one a client added since.
    rerender(
      <QueryClientProvider client={client}>
        <NotificationClassTab
          deviceId={1}
          obj={makeObj({ object_type: "notification-class", notification_recipient_list: [emptyRecipient(), wire] })}
        />
      </QueryClientProvider>
    );
    expect(screen.getByText(/pid 9/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /save/i }));
    expect(vi.mocked(objectsApi.update).mock.lastCall![2]).toEqual({});
  });

  // The object prop stays the pre-save one until the objects refetch lands.
  it("keeps the saved recipients on screen after Save, and a later edit builds on them", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockReset().mockResolvedValue(makeObj());
    renderTab(makeObj({ object_type: "notification-class", notification_recipient_list: [] }));
    await user.click(screen.getByRole("button", { name: /add recipient/i }));
    await user.click(screen.getByRole("button", { name: /save/i }));
    await screen.findByText(/saved/i);
    expect(screen.getAllByRole("button", { name: "Delete" })).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: /add recipient/i }));
    await user.click(screen.getByRole("button", { name: /save/i }));
    await waitFor(() => expect(objectsApi.update).toHaveBeenCalledTimes(2));
    expect(vi.mocked(objectsApi.update).mock.calls[1][2]).toEqual({
      notification_recipient_list: [emptyRecipient(), emptyRecipient()],
    });
  });

  it("keeps the saved priority on screen after Save, and saves a change back to the old value", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockReset().mockResolvedValue(makeObj());
    renderTab(makeObj({ notification_recipient_list: [emptyRecipient()] }));
    const field = screen.getByLabelText("Priority to fault");
    await user.clear(field);
    await user.type(field, "5");
    await user.click(screen.getByRole("button", { name: /save/i }));
    await screen.findByText(/saved/i);
    expect(screen.getByLabelText("Priority to fault")).toHaveValue(5);

    await user.clear(screen.getByLabelText("Priority to fault"));
    await user.type(screen.getByLabelText("Priority to fault"), "100");
    await user.click(screen.getByRole("button", { name: /save/i }));
    await waitFor(() => expect(objectsApi.update).toHaveBeenCalledTimes(2));
    expect(vi.mocked(objectsApi.update).mock.calls[1][2]).toEqual({ notification_priority: [100, 100, 100] });
  });

  it("shows the default priorities and ack required when unset", () => {
    renderTab(makeObj({ notification_priority: null, notification_ack_required: null }));
    for (const name of ["Priority to off-normal", "Priority to fault", "Priority to normal"]) {
      expect(screen.getByLabelText(name)).toHaveValue(100);
    }
    const group = screen.getByRole("group", { name: "Ack required" });
    for (const box of within(group).getAllByRole("checkbox")) expect(box).toBeChecked();
  });

  it("has one priority help line under the three fields", () => {
    renderTab(makeObj());
    const help = screen.getByText(/0 is the highest priority/);
    expect(help.closest(".bp-field")).toBeNull();
  });

  it("sends only the priority when only it changed", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockClear();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(makeObj({ notification_recipient_list: [emptyRecipient()] }));
    const field = screen.getByLabelText("Priority to fault");
    await user.clear(field);
    await user.type(field, "5");
    await user.click(screen.getByRole("button", { name: /save/i }));
    expect(vi.mocked(objectsApi.update).mock.lastCall![2]).toEqual({ notification_priority: [100, 5, 100] });
  });

  it("sends ack required when a transition is unticked", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockClear();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(makeObj());
    await user.click(within(screen.getByRole("group", { name: "Ack required" })).getByLabelText("To normal"));
    await user.click(screen.getByRole("button", { name: /save/i }));
    expect(vi.mocked(objectsApi.update).mock.lastCall![2]).toEqual({ notification_ack_required: [true, true, false] });
  });

  it.each([["clearing", ""], ["256", "256"], ["-1", "-1"], ["1.5", "1.5"]])(
    "refuses a priority of %s and sends nothing",
    async (_what, typed) => {
      const user = userEvent.setup();
      vi.mocked(objectsApi.update).mockClear();
      renderTab(makeObj());
      const field = screen.getByLabelText("Priority to normal");
      await user.clear(field);
      if (typed) fireEvent.change(field, { target: { value: typed } });
      await user.click(screen.getByRole("button", { name: /save/i }));
      expect(objectsApi.update).not.toHaveBeenCalled();
      expect(screen.getByRole("alert")).toHaveTextContent(/priority/i);
      expect(field).toBeInvalid();
    }
  );

  it("puts a save error under Save", async () => {
    vi.mocked(objectsApi.update).mockRejectedValue(new Error("a device recipient requires instance_number"));
    const user = userEvent.setup();
    renderTab(makeObj());
    await user.click(screen.getByRole("button", { name: /add recipient/i }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Couldn't save: a device recipient requires instance_number");
    expect(screen.getByRole("button", { name: "Save" }).closest("form")).toContainElement(alert);
  });

  it("names each recipient row's controls after the row", () => {
    const { container } = renderTab(
      makeObj({
        notification_recipient_list: [
          emptyRecipient(),
          { ...emptyRecipient(), recipient: { kind: "device", instance_number: 17 } },
        ],
      })
    );
    expectNamedControls(container);
    expect(screen.getByRole("combobox", { name: "Recipient 1 kind" })).toHaveValue("address");
    expect(screen.getByRole("textbox", { name: "Recipient 1 address" })).toBeInTheDocument();
    expect(screen.getByRole("spinbutton", { name: "Recipient 2 device instance" })).toHaveValue(17);
    expect(screen.getAllByRole("checkbox", { name: "Confirmed" })).toHaveLength(2);
  });
});

describe("a new recipient's window", () => {
  it("covers the last minute of the day", () => {
    expect(emptyRecipient().from_time).toBe("00:00");
    expect(emptyRecipient().to_time).toBe("23:59:59.99");
  });

  it("is added with that window", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<RecipientListEditor recipients={[]} onChange={onChange} />);
    await user.click(screen.getByRole("button", { name: "Add recipient" }));
    expect(onChange).toHaveBeenCalledWith([expect.objectContaining({ to_time: "23:59:59.99" })]);
  });
});

describe("recipientSummary", () => {
  it("summarizes the defaults", () => {
    expect(recipientSummary(emptyRecipient())).toBe("pid 1 · every day · all day · all transitions");
  });

  it("treats 23:59:59.99 as all day", () => {
    expect(
      recipientSummary({ ...emptyRecipient(), from_time: "00:00:00.00", to_time: "23:59:59.99" }),
    ).toBe("pid 1 · every day · all day · all transitions");
  });

  it("shows a weekday window, a time range and some transitions", () => {
    expect(
      recipientSummary({
        ...emptyRecipient(),
        process_identifier: 5,
        valid_days: [true, true, true, true, true, false, false],
        from_time: "06:00:00.00",
        to_time: "18:00:00.00",
        transitions: [true, false, true],
      }),
    ).toBe("pid 5 · Mon–Fri · 06:00–18:00 device time · to-offnormal, to-normal");
  });

  it("lists scattered days and says when there are none", () => {
    expect(
      recipientSummary({
        ...emptyRecipient(),
        valid_days: [true, false, true, false, false, false, false],
        transitions: [false, false, false],
      }),
    ).toBe("pid 1 · Mon, Wed · all day · no transitions");
    expect(recipientSummary({ ...emptyRecipient(), valid_days: Array(7).fill(false) })).toContain("no days");
  });

  it("renders the summary in each editor row", () => {
    render(<RecipientListEditor recipients={[emptyRecipient()]} onChange={() => {}} />);
    expect(screen.getByText("pid 1 · every day · all day · all transitions")).toBeInTheDocument();
  });

  it("wraps the row summary only between parts or after a comma", () => {
    const summary = recipientSummary(
      { ...emptyRecipient(), valid_days: [true, false, true, false, false, false, false] },
      true,
    );
    expect(summary).toBe("pid 1 · Mon, Wed · all day · all transitions");
  });
});
