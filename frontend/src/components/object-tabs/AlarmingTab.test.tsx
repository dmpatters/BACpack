// Covers AlarmFieldsEditor, the type-conditional alarm-parameter form (its pure
// rendering/change logic, matching ConnectorDetail.test.tsx's JsonPathPicker
// precedent), and the connected AlarmingTab with objectsApi mocked: a rejected
// save (e.g. a 400 for a bad limit) and the catch-22 coverage below (enabling
// alarming needed limits, but the tab only showed once it was enabled). The
// rest of the tab is API-driven (TanStack Query save) and covered at the
// backend/API level (tests/test_application.py,
// tests/web/test_objects_alarming.py) plus manual verification.
import { useState } from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import { AlarmFieldsEditor, shownLimitEnable } from "./AlarmingTab";
import AlarmingTab from "./AlarmingTab";
import type { AlarmFieldsValue } from "./AlarmingTab";
import { columnOf } from "../../testing/columns";
import { expectNamedControls } from "../../testing/controls";
import { objectsApi } from "../../api/endpoints";
import type { BacnetObjectDTO } from "../../api/types";

vi.mock("../../api/endpoints", () => ({
  objectsApi: {
    update: vi.fn(),
  },
}));

// AlarmFieldsEditor is a controlled component: its displayed value comes
// entirely from the `value` prop. userEvent.type against a bare no-op
// onChange would only ever register the last keystroke (React resets the
// DOM back to the stale controlled value between keystrokes), so the
// typing-based assertions below drive the editor through this tiny
// stateful harness -- the same feedback loop AlarmingTab itself provides
// via its own `form`/`setForm` state -- rather than exercising it through
// an onChange that goes nowhere.
function ControlledEditor({
  objectType,
  initial,
  onChange,
}: {
  objectType: string;
  initial: AlarmFieldsValue;
  onChange: (next: AlarmFieldsValue) => void;
}) {
  const [value, setValue] = useState(initial);
  return (
    <AlarmFieldsEditor
      objectType={objectType}
      value={value}
      onChange={(next) => {
        setValue(next);
        onChange(next);
      }}
    />
  );
}

function baseValue(): AlarmFieldsValue {
  return {
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
  };
}

describe("AlarmFieldsEditor", () => {
  it("toggles event detection", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<ControlledEditor objectType="analog-input" initial={baseValue()} onChange={onChange} />);

    const checkbox = screen.getByLabelText("Event detection enabled");
    expect(checkbox).toBeChecked();
    await user.click(checkbox);

    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ alarm_event_detection_enable: false })
    );
  });

  it("renders high/low limit fields for an analog-input object", () => {
    render(<AlarmFieldsEditor objectType="analog-input" value={baseValue()} onChange={() => {}} />);
    expect(screen.getByLabelText("High limit")).toBeInTheDocument();
    expect(screen.getByLabelText("Low limit")).toBeInTheDocument();
  });

  it("does not render analog fields for a binary-input object, renders alarm value instead", () => {
    render(<AlarmFieldsEditor objectType="binary-input" value={baseValue()} onChange={() => {}} />);
    expect(screen.queryByLabelText("High limit")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Alarm value")).toBeInTheDocument();
  });

  it("renders the alarm state numbers field for a multi-state-value object", () => {
    render(<AlarmFieldsEditor objectType="multi-state-value" value={baseValue()} onChange={() => {}} />);
    expect(screen.getByLabelText("Alarm state numbers")).toBeInTheDocument();
  });

  it("calls onChange with the updated high limit", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<ControlledEditor objectType="analog-input" initial={baseValue()} onChange={onChange} />);

    await user.type(screen.getByLabelText("High limit"), "100");

    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ alarm_high_limit: 100 })
    );
  });

  it("parses comma-separated alarm state numbers into an array", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <ControlledEditor objectType="multi-state-value" initial={baseValue()} onChange={onChange} />
    );

    await user.type(screen.getByLabelText("Alarm state numbers"), "2, 3");

    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ alarm_values: [2, 3] })
    );
  });

  // Clearing a previously-set high/low limit must produce null (a genuine "no
  // limit" state), not 0 -- otherwise a user has no way to express a one-sided
  // limit through the UI (Number("") === 0, not null). Load-bearing for the
  // backend, which derives limitEnable from which limits are actually set.
  it("clears alarm_high_limit to null (not 0) when the field is emptied", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <ControlledEditor
        objectType="analog-input"
        initial={{ ...baseValue(), alarm_high_limit: 100 }}
        onChange={onChange}
      />
    );

    await user.clear(screen.getByLabelText("High limit"));

    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ alarm_high_limit: null })
    );
  });

  it("clears alarm_low_limit to null (not 0) when the field is emptied", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <ControlledEditor
        objectType="analog-input"
        initial={{ ...baseValue(), alarm_low_limit: 0 }}
        onChange={onChange}
      />
    );

    await user.clear(screen.getByLabelText("Low limit"));

    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ alarm_low_limit: null })
    );
  });

  // Number("") is 0, so emptying the field must not save notification
  // class 0 -- a different (usually nonexistent) class.
  it("clears notification_class_instance to null (not 0) when the field is emptied", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <ControlledEditor
        objectType="analog-input"
        initial={{ ...baseValue(), notification_class_instance: 5 }}
        onChange={onChange}
      />
    );

    await user.clear(screen.getByLabelText("Notification Class instance number"));

    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ notification_class_instance: null })
    );
  });

  // The "none" option's value "" isn't "true": picking it must clear the
  // alarm value, not save inactive.
  it("sets alarm_value to null when the none option is picked", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <ControlledEditor
        objectType="binary-input"
        initial={{ ...baseValue(), alarm_value: true }}
        onChange={onChange}
      />
    );

    await user.selectOptions(screen.getByLabelText("Alarm value"), "");

    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ alarm_value: null }));
  });
});

// --- AlarmingTab as a tab panel: driven by props, not a route -----------

function makeObj(overrides: Partial<BacnetObjectDTO> = {}): BacnetObjectDTO {
  return {
    id: 5,
    device_id: 599002,
    object_type: "analog-input",
    instance_number: 1,
    name: "AI-1",
    description: null,
    units: "degreesFahrenheit",
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
    alarming_enabled: true,
    notification_class_instance: 1,
    alarm_time_delay: 0,
    alarm_time_delay_normal: null,
    alarm_notify_type: "alarm",
    alarm_event_enable: null,
    alarm_high_limit: 100,
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
      <AlarmingTab deviceId={599002} obj={obj} />
    </QueryClientProvider>
  );
}

describe("AlarmingTab", () => {
  it("shows the server's error message when the save is rejected", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockRejectedValue(
      new Error("at least one of alarm_high_limit/alarm_low_limit is required")
    );

    renderTab(makeObj());

    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(
        screen.getByText("at least one of alarm_high_limit/alarm_low_limit is required")
      ).toBeInTheDocument();
    });
  });

  // The tab must be usable before alarming is enabled, or there is no way to
  // enable it with valid limits: it has to be reachable regardless of the
  // flag's current value.
  it("is usable before alarming has ever been enabled", () => {
    renderTab(makeObj({ object_type: "analog-input", alarming_enabled: false }));
    expect(screen.getByLabelText("High limit")).toBeInTheDocument();
  });

  it("stays on the tab after a successful save instead of navigating away", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(makeObj({ object_type: "analog-input", alarming_enabled: false }));
    await user.click(screen.getByRole("button", { name: /save/i }));
    expect(await screen.findByText(/saved/i)).toBeInTheDocument();
  });

  it("says a running device applies the save", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(makeObj({ object_type: "analog-input", alarming_enabled: false }));
    await user.click(screen.getByRole("button", { name: /save/i }));
    expect(await screen.findByText("Saved.")).toBeInTheDocument();
  });

  // With ObjectRow.tsx's grid checkbox gone, this tab is the only
  // single-object way to flip alarming_enabled -- confirm it's actually here,
  // reflects the object's current value, and is included in what gets saved.
  it("shows an unchecked alarming-enabled checkbox when the object has it off", () => {
    renderTab(makeObj({ alarming_enabled: false }));
    expect(screen.getByLabelText(/alarming enabled/i)).not.toBeChecked();
  });

  it("shows a checked alarming-enabled checkbox when the object has it on", () => {
    renderTab(makeObj({ alarming_enabled: true }));
    expect(screen.getByLabelText(/alarming enabled/i)).toBeChecked();
  });

  it("includes alarming_enabled in the saved patch when the checkbox is toggled on", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(makeObj({ object_type: "analog-input", alarming_enabled: false }));

    await user.click(screen.getByLabelText(/alarming enabled/i));
    await user.click(screen.getByRole("button", { name: /save/i }));

    expect(objectsApi.update).toHaveBeenCalledWith(
      599002,
      5,
      expect.objectContaining({ alarming_enabled: true })
    );
  });

  // A BACnet client can write alarm settings while this tab is open; saving
  // must not put back values the user never touched here.
  it("saves only the alarm fields the user changed, plus alarming_enabled", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(makeObj({ object_type: "analog-input", alarming_enabled: true }));

    await user.type(screen.getByLabelText("Time delay (seconds)"), "5");
    await user.click(screen.getByRole("button", { name: /save/i }));

    const patch = vi.mocked(objectsApi.update).mock.lastCall![2];
    expect(Object.keys(patch).sort()).toEqual(["alarm_time_delay", "alarming_enabled"]);
    expect(patch).toMatchObject({ alarm_time_delay: 5, alarming_enabled: true });
  });

  it("does not resend a stale value on a second save after the object refreshed", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const tab = (obj: BacnetObjectDTO) => (
      <QueryClientProvider client={client}>
        <AlarmingTab deviceId={599002} obj={obj} />
      </QueryClientProvider>
    );
    const { rerender } = render(tab(makeObj({ alarm_high_limit: 100 })));

    await user.type(screen.getByLabelText("Time delay (seconds)"), "5");
    await user.click(screen.getByRole("button", { name: /save/i }));
    await waitFor(() => expect(screen.getByText(/^Saved\.$/)).toBeInTheDocument());

    // A BACnet client lowered the high limit; the object query refreshed.
    rerender(tab(makeObj({ alarm_high_limit: 90, alarm_time_delay: 5 })));
    await user.type(screen.getByLabelText("Time delay (seconds)"), "0");
    await user.click(screen.getByRole("button", { name: /save/i }));

    const patch = vi.mocked(objectsApi.update).mock.lastCall![2];
    expect(Object.keys(patch).sort()).toEqual(["alarm_time_delay", "alarming_enabled"]);
  });

  it("includes alarming_enabled: false in the saved patch when left untouched on an already-off object", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(makeObj({ object_type: "analog-input", alarming_enabled: false }));

    await user.click(screen.getByRole("button", { name: /save/i }));

    expect(objectsApi.update).toHaveBeenCalledWith(
      599002,
      5,
      expect.objectContaining({ alarming_enabled: false })
    );
  });

  it("puts a save error under Save", async () => {
    vi.mocked(objectsApi.update).mockRejectedValue(new Error("notification class 9 doesn't exist"));
    const user = userEvent.setup();
    renderTab(makeObj());
    await user.click(screen.getByRole("button", { name: "Save" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Couldn't save: notification class 9 doesn't exist");
    expect(screen.getByRole("button", { name: "Save" }).closest("form")).toContainElement(alert);
  });

  it.each(["analog-input", "binary-value", "multi-state-value"])("names every control on a %s", (objectType) => {
    const { container } = renderTab(makeObj({ object_type: objectType }));
    expectNamedControls(container);
  });
});

// --- Multi-state alarm state numbers ----------------------------------------

describe("Alarm state numbers", () => {
  it.each(["1 2", "1;2", "1, 2", " 1 ,; 2 "])("reads %j as states 1 and 2", async (typed) => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<ControlledEditor objectType="multi-state-value" initial={baseValue()} onChange={onChange} />);

    await user.type(screen.getByLabelText("Alarm state numbers"), typed);

    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ alarm_values: [1, 2] }));
  });

  it("refuses to save a list holding something that isn't a state number", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockClear();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(makeObj({ object_type: "multi-state-value", alarm_values: [3] }));

    const field = screen.getByLabelText("Alarm state numbers");
    await user.clear(field);
    await user.type(field, "2, x");
    await user.click(screen.getByRole("button", { name: /save/i }));

    expect(screen.getByText(/"x" is not a state number/)).toBeInTheDocument();
    expect(objectsApi.update).not.toHaveBeenCalled();
  });

  it("shows the saved list after a save", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const tab = (obj: BacnetObjectDTO) => (
      <QueryClientProvider client={client}>
        <AlarmingTab deviceId={599002} obj={obj} />
      </QueryClientProvider>
    );
    const { rerender } = render(tab(makeObj({ object_type: "multi-state-value", alarm_values: [3] })));

    const field = screen.getByLabelText("Alarm state numbers");
    await user.clear(field);
    await user.type(field, "1 2");
    await user.click(screen.getByRole("button", { name: /save/i }));
    await waitFor(() => expect(screen.getByText(/^Saved\.$/)).toBeInTheDocument());
    rerender(tab(makeObj({ object_type: "multi-state-value", alarm_values: [1, 2] })));

    expect(screen.getByLabelText("Alarm state numbers")).toHaveValue("1, 2");
  });
});

describe("AlarmingTab between Save and the objects refetch", () => {
  // The object prop stays the pre-save one until the refetch lands.
  it("keeps the saved values on screen, and saves a change back to the old value", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockReset().mockResolvedValue(makeObj());
    renderTab(makeObj({ alarm_high_limit: 100 }));

    const high = screen.getByLabelText("High limit");
    await user.clear(high);
    await user.type(high, "90");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText(/^Saved\.$/);
    expect(screen.getByLabelText("High limit")).toHaveValue(90);
    expect(screen.getByRole("checkbox", { name: "Alarming enabled" })).toBeChecked();

    await user.clear(screen.getByLabelText("High limit"));
    await user.type(screen.getByLabelText("High limit"), "100");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(objectsApi.update).toHaveBeenCalledTimes(2));
    expect(vi.mocked(objectsApi.update).mock.calls[1][2]).toMatchObject({ alarm_high_limit: 100 });
  });

  it("saves only what changed since the last save, not since the stale object", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockReset().mockResolvedValue(makeObj());
    renderTab(makeObj({ alarm_high_limit: 100 }));

    await user.clear(screen.getByLabelText("High limit"));
    await user.type(screen.getByLabelText("High limit"), "90");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText(/^Saved\.$/);

    await user.click(screen.getByRole("checkbox", { name: "Alarming enabled" }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(objectsApi.update).toHaveBeenCalledTimes(2));
    const second = vi.mocked(objectsApi.update).mock.calls[1][2];
    expect(second).toEqual({ alarming_enabled: false });
  });
});

describe("AlarmFieldsEditor alarm controls", () => {
  it("shows deadband, limit enables and time delay to normal for an analog value", () => {
    render(<AlarmFieldsEditor objectType="analog-value" value={baseValue()} onChange={() => {}} />);
    expect(screen.getByLabelText("Deadband")).toBeInTheDocument();
    expect(screen.getByLabelText("High limit enabled")).toBeInTheDocument();
    expect(screen.getByLabelText("Low limit enabled")).toBeInTheDocument();
    expect(screen.getByLabelText("Time delay to normal (seconds)")).toBeInTheDocument();
  });

  it("shows only time delay to normal for a binary value", () => {
    render(<AlarmFieldsEditor objectType="binary-value" value={baseValue()} onChange={() => {}} />);
    expect(screen.queryByLabelText("Deadband")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("High limit enabled")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Low limit enabled")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Time delay to normal (seconds)")).toBeInTheDocument();
  });

  it("follows the limits while limit enable is unset", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<ControlledEditor objectType="analog-value" initial={baseValue()} onChange={onChange} />);

    await user.type(screen.getByLabelText("High limit"), "80");

    expect(screen.getByLabelText("High limit enabled")).toBeChecked();
    expect(screen.getByLabelText("Low limit enabled")).not.toBeChecked();
    expect(screen.getByLabelText("Low limit enabled")).toBeDisabled();
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ alarm_limit_enable: null }));
  });

  it("saves an explicit limit enable once a box is clicked", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <ControlledEditor
        objectType="analog-value"
        initial={{ ...baseValue(), alarm_high_limit: 80, alarm_low_limit: 20 }}
        onChange={onChange}
      />
    );

    await user.click(screen.getByLabelText("Low limit enabled"));

    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ alarm_limit_enable: [false, true] }));
  });

  it("keeps an explicit limit enable while its limit is cleared and retyped", async () => {
    // A number input reports "" mid-edit, so the enable is settled at save.
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <ControlledEditor
        objectType="analog-value"
        initial={{ ...baseValue(), alarm_high_limit: 80, alarm_low_limit: 20, alarm_limit_enable: [true, true] }}
        onChange={onChange}
      />
    );

    await user.clear(screen.getByLabelText("Low limit"));
    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ alarm_low_limit: null, alarm_limit_enable: [true, true] })
    );
    await user.type(screen.getByLabelText("Low limit"), "-5");
    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ alarm_low_limit: -5, alarm_limit_enable: [true, true] })
    );
    expect(screen.getByLabelText("Low limit enabled")).toBeChecked();
  });

  it("sends null when time delay to normal is cleared", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <ControlledEditor
        objectType="analog-value"
        initial={{ ...baseValue(), alarm_time_delay_normal: 30 }}
        onChange={onChange}
      />
    );

    await user.clear(screen.getByLabelText("Time delay to normal (seconds)"));

    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ alarm_time_delay_normal: null }));
  });

  it("shows every transition enabled when event enable is unset", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<ControlledEditor objectType="analog-value" initial={baseValue()} onChange={onChange} />);

    const group = screen.getByRole("group", { name: "Event enable" });
    for (const box of within(group).getAllByRole("checkbox")) expect(box).toBeChecked();
    await user.click(within(group).getByLabelText("To normal"));

    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ alarm_event_enable: [true, true, false] })
    );
  });

  it("takes a fractional deadband", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<ControlledEditor objectType="analog-value" initial={baseValue()} onChange={onChange} />);
    const field = screen.getByLabelText("Deadband");
    expect(field).toHaveAttribute("step", "any");
    await user.type(field, "0.5");
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ alarm_deadband: 0.5 }));
    expect(field).toBeValid();
  });

  it("lets a ticked limit enable with a blank limit be unticked", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <ControlledEditor
        objectType="analog-value"
        initial={{ ...baseValue(), alarm_high_limit: 80, alarm_limit_enable: [true, false] }}
        onChange={onChange}
      />
    );
    const box = screen.getByLabelText("Low limit enabled");
    expect(box).toBeChecked();
    expect(box).toBeEnabled();
    await user.click(box);
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ alarm_limit_enable: [false, false] }));
  });

  it("sets the notify type", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<ControlledEditor objectType="analog-value" initial={baseValue()} onChange={onChange} />);

    await user.selectOptions(screen.getByLabelText("Notify type"), "event");

    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ alarm_notify_type: "event" }));
  });

  it("shownLimitEnable follows the limits only while the column is unset", () => {
    expect(shownLimitEnable({ ...baseValue(), alarm_high_limit: 5 })).toEqual([false, true]);
    expect(shownLimitEnable({ ...baseValue(), alarm_low_limit: 0 })).toEqual([true, false]);
    expect(shownLimitEnable({ ...baseValue(), alarm_high_limit: 5, alarm_limit_enable: [false, false] })).toEqual([
      false,
      false,
    ]);
  });
});

describe("AlarmingTab alarm controls", () => {
  it("puts notify type and event enable in the Notification panel, under Limits", () => {
    renderTab(makeObj({ object_type: "analog-input" }));
    const notification = screen.getByRole("group", { name: "Notification" });
    expect(within(notification).getByLabelText("Notify type")).toBeInTheDocument();
    expect(within(notification).getByRole("group", { name: "Event enable" })).toBeInTheDocument();
    const limits = screen.getByRole("group", { name: "Limits" });
    expect(within(limits).getByLabelText("Deadband")).toBeInTheDocument();
    expect(limits.compareDocumentPosition(notification) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("sends both the cleared limit and the unticked enable on a save", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockClear();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(makeObj({ alarm_high_limit: 100, alarm_low_limit: 10, alarm_limit_enable: [true, true] }));

    await user.clear(screen.getByLabelText("Low limit"));
    await user.click(screen.getByRole("button", { name: /save/i }));

    const patch = vi.mocked(objectsApi.update).mock.lastCall![2];
    expect(patch).toMatchObject({ alarm_low_limit: null, alarm_limit_enable: [false, true] });
    expect(patch).not.toHaveProperty("alarm_event_enable");
  });

  it("keeps an explicit limit enable when a limit is retyped, and saves only the new limit", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockReset().mockResolvedValue(makeObj());
    renderTab(makeObj({ alarm_high_limit: 100, alarm_low_limit: 10, alarm_limit_enable: [true, true] }));

    await user.clear(screen.getByLabelText("High limit"));
    await user.type(screen.getByLabelText("High limit"), "90");
    await user.clear(screen.getByLabelText("Low limit"));
    await user.type(screen.getByLabelText("Low limit"), "-5");
    await user.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => expect(objectsApi.update).toHaveBeenCalledTimes(1));
    const patch = vi.mocked(objectsApi.update).mock.lastCall![2];
    expect(patch).toMatchObject({ alarm_high_limit: 90, alarm_low_limit: -5 });
    expect(patch).not.toHaveProperty("alarm_limit_enable");
    expect(screen.getByLabelText("High limit enabled")).toBeChecked();
    expect(screen.getByLabelText("Low limit enabled")).toBeChecked();
  });

  it("shows a cleared limit's side unticked after the save, with nothing left unsaved", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockReset().mockResolvedValue(makeObj());
    renderTab(makeObj({ alarm_high_limit: 100, alarm_low_limit: 10, alarm_limit_enable: [true, true] }));

    await user.clear(screen.getByLabelText("Low limit"));
    await user.click(screen.getByRole("button", { name: /save/i }));

    await screen.findByText(/^Saved\.$/);
    expect(screen.getByLabelText("Low limit enabled")).not.toBeChecked();
    expect(screen.getByLabelText("High limit enabled")).toBeChecked();
  });

  it("sends no event enable after a tick off and back on", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockClear();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(makeObj({ alarm_event_enable: null }));
    await user.click(screen.getByLabelText("To fault"));
    await user.click(screen.getByLabelText("To fault"));
    await user.click(screen.getByRole("button", { name: /save/i }));
    expect(vi.mocked(objectsApi.update).mock.lastCall![2]).not.toHaveProperty("alarm_event_enable");
  });

  it("saves only the notify type when that is all that changed", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockClear();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(makeObj({ object_type: "analog-input", alarming_enabled: true }));

    await user.selectOptions(screen.getByLabelText("Notify type"), "event");
    await user.click(screen.getByRole("button", { name: /save/i }));

    const patch = vi.mocked(objectsApi.update).mock.lastCall![2];
    expect(Object.keys(patch).sort()).toEqual(["alarm_notify_type", "alarming_enabled"]);
    expect(patch).toMatchObject({ alarm_notify_type: "event", alarming_enabled: true });
  });
});

describe("AlarmingTab layout", () => {
  it("stacks Limits and Notification in one form column, with Alarming enabled above both", () => {
    renderTab(makeObj({ object_type: "analog-input" }));
    for (const field of [
      screen.getByLabelText("High limit"),
      screen.getByLabelText("Time delay (seconds)"),
      screen.getByLabelText("Notification Class instance number"),
      screen.getByRole("checkbox", { name: "Alarming enabled" }),
    ]) {
      expect(columnOf(field)).toBeNull();
    }
    expect(screen.getByLabelText("High limit").closest("form")).not.toHaveClass("bp-form--wide");
  });
});
