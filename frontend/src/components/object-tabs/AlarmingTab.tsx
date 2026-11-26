import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { objectsApi } from "../../api/endpoints";
import type { BacnetObjectDTO } from "../../api/types";
import Button from "../Button";
import { CheckField, Field, Fieldset, Form, FormActions, FormGrid } from "../Form";
import { ActionStatus } from "../AsyncState";
import { differs, useDraft, useUnsavedChanges } from "../UnsavedChanges";
import { ANALOG_TYPES } from "../../utils/objectTypes";
import { TransitionChecks, transitionsOrDefault } from "../TransitionChecks";

const BINARY_TYPES = new Set(["binary-input", "binary-value"]);
const MULTISTATE_TYPES = new Set(["multi-state-input", "multi-state-value"]);

/** State numbers separated by commas, semicolons or spaces; `bad` is the
 * first piece that isn't a whole number. */
export function parseStateNumbers(text: string): { values: number[]; bad: string | null } {
  const pieces = text.split(/[\s,;]+/).filter((piece) => piece !== "");
  const bad = pieces.find((piece) => !/^\d+$/.test(piece)) ?? null;
  return { values: pieces.filter((piece) => /^\d+$/.test(piece)).map(Number), bad };
}

/** The type-conditional alarm-parameter fields -- a pure, presentational
 * component (no API/router dependency) so it can be unit-tested directly,
 * same shape as ConnectorDetail.tsx's exported JsonPathPicker. */
export interface AlarmFieldsValue {
  notification_class_instance: number | null;
  alarm_time_delay: number;
  alarm_time_delay_normal: number | null;
  alarm_notify_type: "alarm" | "event";
  alarm_event_enable: [boolean, boolean, boolean] | null;
  alarm_high_limit: number | null;
  alarm_low_limit: number | null;
  alarm_deadband: number;
  alarm_limit_enable: [boolean, boolean] | null;
  alarm_value: boolean | null;
  alarm_values: number[] | null;
  alarm_event_detection_enable: boolean;
}

/** The Limit_Enable pair `[low, high]` the form shows. While the column is
 * unset it follows the limits, as the device does when it builds the object. */
export function shownLimitEnable(value: AlarmFieldsValue): [boolean, boolean] {
  return value.alarm_limit_enable ?? [value.alarm_low_limit !== null, value.alarm_high_limit !== null];
}

/** An explicit Limit_Enable with a side whose limit is empty turns that side
 * off: the save would otherwise be refused as "enabled with no value". */
export function withoutEmptySides<T extends AlarmFieldsValue>(value: T): T {
  const enable = value.alarm_limit_enable;
  if (enable === null) return value;
  const fitted: [boolean, boolean] = [enable[0] && value.alarm_low_limit !== null, enable[1] && value.alarm_high_limit !== null];
  return fitted[0] === enable[0] && fitted[1] === enable[1] ? value : { ...value, alarm_limit_enable: fitted };
}

// `part` picks one of the tab's two groups; without it both render.
export function AlarmFieldsEditor({
  objectType,
  value,
  onChange,
  part,
}: {
  objectType: string;
  value: AlarmFieldsValue;
  onChange: (next: AlarmFieldsValue) => void;
  part?: "limits" | "notification";
}) {
  const showLimits = part !== "notification";
  const showNotification = part !== "limits";
  const isAnalog = ANALOG_TYPES.includes(objectType);
  const isBinary = BINARY_TYPES.has(objectType);
  const isMultiState = MULTISTATE_TYPES.has(objectType);

  // Local raw-text buffer for the comma-separated field, decoupled from the
  // parsed-array round-trip (join(", ")) so an in-progress "," or trailing
  // space the user just typed isn't immediately reformatted away on every
  // keystroke -- that reformatting-on-every-keystroke would otherwise fight
  // with the user's own typing (classic "formatted input" pitfall).
  const [alarmValuesText, setAlarmValuesText] = useState(() => (value.alarm_values ?? []).join(", "));
  const [alarmValuesError, setAlarmValuesError] = useState<string | null>(null);

  // Limit_Enable is left alone here: a number input reports "" mid-edit
  // (cleared, or just "-"), so unticking on "" would lose the enable while
  // a limit is retyped. withoutEmptySides settles it at save.
  const withLimit = (side: "alarm_low_limit" | "alarm_high_limit", text: string): AlarmFieldsValue => ({
    ...value,
    [side]: text === "" ? null : Number(text),
  });
  const toggleLimitEnable = (slot: 0 | 1, on: boolean) => {
    const enable: [boolean, boolean] = [...shownLimitEnable(value)];
    enable[slot] = on;
    onChange({ ...value, alarm_limit_enable: enable });
  };
  const limitEnable = shownLimitEnable(value);

  return (
    <>
      {showNotification && (
        <Field label="Notification Class instance number">
          <input
            id="notification_class_instance"
            type="number"
            value={value.notification_class_instance ?? ""}
            onChange={(e) =>
              onChange({
                ...value,
                notification_class_instance: e.target.value === "" ? null : Number(e.target.value),
              })
            }
          />
        </Field>
      )}
      {showNotification && (
        <Field label="Notify type">
          <select
            id="alarm_notify_type"
            value={value.alarm_notify_type}
            onChange={(e) => onChange({ ...value, alarm_notify_type: e.target.value as "alarm" | "event" })}
          >
            <option value="alarm">Alarm</option>
            <option value="event">Event</option>
          </select>
        </Field>
      )}
      {showNotification && (
        <TransitionChecks
          legend="Event enable"

          idPrefix="alarm_event_enable"
          value={transitionsOrDefault(value.alarm_event_enable)}
          onChange={(next) => onChange({ ...value, alarm_event_enable: next })}
        />
      )}
      {showLimits && (
        <Field label="Time delay (seconds)">
          <input
            id="alarm_time_delay"
            type="number"
            min={0}
            value={value.alarm_time_delay}
            onChange={(e) => onChange({ ...value, alarm_time_delay: Number(e.target.value) })}
          />
        </Field>
      )}
      {showLimits && (
        <Field label="Time delay to normal (seconds)" help="Blank uses Time delay.">
          <input
            id="alarm_time_delay_normal"
            type="number"
            min={0}
            value={value.alarm_time_delay_normal ?? ""}
            onChange={(e) =>
              onChange({ ...value, alarm_time_delay_normal: e.target.value === "" ? null : Number(e.target.value) })
            }
          />
        </Field>
      )}

      {showLimits && (
        <CheckField
          label="Event detection enabled"
          checked={value.alarm_event_detection_enable}
          onChange={(e) => onChange({ ...value, alarm_event_detection_enable: e.target.checked })}
        />
      )}

      {showLimits && isAnalog && (
        <FormGrid>
          <Field label="High limit">
            <input
              id="alarm_high_limit"
              type="number"
              value={value.alarm_high_limit ?? ""}
              onChange={(e) => onChange(withLimit("alarm_high_limit", e.target.value))}
            />
          </Field>
          <Field label="Low limit">
            <input
              id="alarm_low_limit"
              type="number"
              value={value.alarm_low_limit ?? ""}
              onChange={(e) => onChange(withLimit("alarm_low_limit", e.target.value))}
            />
          </Field>
        </FormGrid>
      )}

      {showLimits && isAnalog && (
        <Field label="Deadband">
          <input
            id="alarm_deadband"
            type="number"
            min={0}
            step="any"
            value={value.alarm_deadband}
            onChange={(e) => onChange({ ...value, alarm_deadband: Number(e.target.value) })}
          />
        </Field>
      )}

      {showLimits && isAnalog && (
        <Fieldset legend="Limit enable">
          <CheckField
            label="High limit enabled"
            checked={limitEnable[1]}
            disabled={value.alarm_high_limit === null && !limitEnable[1]}
            onChange={(e) => toggleLimitEnable(1, e.target.checked)}
          />
          <CheckField
            label="Low limit enabled"
            checked={limitEnable[0]}
            disabled={value.alarm_low_limit === null && !limitEnable[0]}
            onChange={(e) => toggleLimitEnable(0, e.target.checked)}
          />
        </Fieldset>
      )}

      {showLimits && isBinary && (
        <Field label="Alarm value">
          <select
            id="alarm_value"
            value={value.alarm_value === null ? "" : String(value.alarm_value)}
            onChange={(e) =>
              onChange({ ...value, alarm_value: e.target.value === "" ? null : e.target.value === "true" })
            }
          >
            <option value="">None</option>
            <option value="true">active</option>
            <option value="false">inactive</option>
          </select>
        </Field>
      )}

      {showLimits && isMultiState && (
        <Field label="Alarm state numbers" help="Separated by commas or spaces." error={alarmValuesError}>
          <input
            id="alarm_values"
            value={alarmValuesText}
            onChange={(e) => {
              const { values, bad } = parseStateNumbers(e.target.value);
              const error = bad === null ? null : `"${bad}" is not a state number`;
              // The form's submit checks validity, so a bad list never saves.
              e.target.setCustomValidity(error ?? "");
              setAlarmValuesError(error);
              setAlarmValuesText(e.target.value);
              onChange({ ...value, alarm_values: values });
            }}
          />
        </Field>
      )}
    </>
  );
}

function savedAlarmFields(obj: BacnetObjectDTO): AlarmFieldsValue {
  return {
    notification_class_instance: obj.notification_class_instance,
    alarm_time_delay: obj.alarm_time_delay,
    alarm_time_delay_normal: obj.alarm_time_delay_normal,
    alarm_notify_type: obj.alarm_notify_type,
    alarm_event_enable: obj.alarm_event_enable,
    alarm_high_limit: obj.alarm_high_limit,
    alarm_low_limit: obj.alarm_low_limit,
    alarm_deadband: obj.alarm_deadband,
    alarm_limit_enable: obj.alarm_limit_enable,
    alarm_value: obj.alarm_value,
    alarm_values: obj.alarm_values,
    alarm_event_detection_enable: obj.alarm_event_detection_enable,
  };
}

type AlarmingDraft = AlarmFieldsValue & { alarming_enabled: boolean };

function savedDraft(obj: BacnetObjectDTO): AlarmingDraft {
  return { ...savedAlarmFields(obj), alarming_enabled: obj.alarming_enabled };
}

export default function AlarmingTab({
  deviceId,
  obj,
}: {
  deviceId: number;
  obj: BacnetObjectDTO;
}) {
  const queryClient = useQueryClient();

  // The draft and what the server last confirmed (the object, or what a save
  // just sent): a refetch refreshes only the fields not edited since. The one
  // place to turn alarming on for a single object (BulkEditPanel does several)
  // is here; alarming_enabled isn't a field of the limit/notification editor's
  // value, but is one more key of the draft.
  const { form: draft, setForm: setDraft, baseline, setBaseline } = useDraft(obj, savedDraft);
  const value: AlarmFieldsValue = draft;
  const alarmingEnabledValue = draft.alarming_enabled;
  const setForm = (next: AlarmFieldsValue) => setDraft((d) => ({ ...d, ...next, alarming_enabled: d.alarming_enabled }));
  // Remounts the editor after a save, so its text fields show what was saved.
  const [saveCount, setSaveCount] = useState(0);

  const save = useMutation({
    mutationFn: ({ patch }: { patch: Record<string, unknown>; sent: AlarmingDraft }) =>
      objectsApi.update(deviceId, obj.id, patch),
    onSuccess: (_result, { sent }) => {
      queryClient.invalidateQueries({ queryKey: ["objects", deviceId] });
      setSaveCount((count) => count + 1);
      setBaseline(sent);
      // Shows a side the save turned off, so the form isn't left dirty.
      setDraft((d) => withoutEmptySides(d));
    },
  });

  // An event enable that was never set is all transitions on, so ticking one
  // off and back on is no change.
  const settled = <T extends AlarmFieldsValue>(v: T): T => ({
    ...v,
    alarm_event_enable: transitionsOrDefault(v.alarm_event_enable),
  });
  const dirty = differs(settled(draft), settled(baseline));
  useUnsavedChanges(dirty, `/devices/${deviceId}/objects/${obj.id}`);

  return (
    <div>
      <Form
        onSubmit={(e) => {
          e.preventDefault();
          if (!e.currentTarget.checkValidity()) return;
          // Only the fields changed here: a BACnet client may have written
          // others since this tab loaded, and saving the whole form would
          // put the old values back. alarming_enabled is always sent.
          const fitted = withoutEmptySides(draft);
          const saved = settled(baseline);
          const shown = settled(fitted);
          const changed = Object.fromEntries(
            (Object.keys(savedAlarmFields(obj)) as (keyof AlarmFieldsValue)[])
              .filter((k) => JSON.stringify(shown[k]) !== JSON.stringify(saved[k]))
              .map((k) => [k, fitted[k]])
          );
          save.mutate({ patch: { ...changed, alarming_enabled: alarmingEnabledValue }, sent: fitted });
        }}
      >
        <CheckField
          label="Alarming enabled"
          checked={alarmingEnabledValue}
          onChange={(e) => setDraft((d) => ({ ...d, alarming_enabled: e.target.checked }))}
        />

          <Fieldset legend="Limits">
            <AlarmFieldsEditor
              key={`${saveCount}:${JSON.stringify(obj.alarm_values)}`}
              part="limits"
              objectType={obj.object_type}
              value={value}
              onChange={setForm}
            />
          </Fieldset>
          <Fieldset legend="Notification">
            <AlarmFieldsEditor part="notification" objectType={obj.object_type} value={value} onChange={setForm} />
          </Fieldset>
        <FormActions>
          <Button variant="primary" type="submit" disabled={save.isPending}>
            Save
          </Button>
        </FormActions>
        <ActionStatus
          action={save}
          failed="Couldn't save"
          done="Saved."
          hideDone={dirty}
        />
      </Form>
    </div>
  );
}
