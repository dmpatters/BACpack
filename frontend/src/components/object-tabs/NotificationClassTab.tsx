import { useMutation, useQueryClient } from "@tanstack/react-query";
import { objectsApi } from "../../api/endpoints";
import type { BacnetObjectDTO, NotificationRecipientEntry } from "../../api/types";
import { WEEKDAY_SHORT } from "../../utils/calendarDates";
import { changedFields } from "../../utils/objectForm";
import Button from "../Button";
import { Field, Fieldset, Form, FormActions, FormGrid, useSubmitProblem } from "../Form";
import { TransitionChecks, transitionsOrDefault, type Transitions } from "../TransitionChecks";
import { ActionStatus } from "../AsyncState";
import { differs, useDraft, useUnsavedChanges } from "../UnsavedChanges";

// What the device serves while the column is unset.
const DEFAULT_PRIORITY = [100, 100, 100];
const PRIORITY_LABELS = ["Priority to off-normal", "Priority to fault", "Priority to normal"];

function priorityProblem(text: string): string | null {
  return /^\d+$/.test(text) && Number(text) <= 255 ? null : "A priority is a whole number from 0 to 255.";
}

export function emptyRecipient(): NotificationRecipientEntry {
  return {
    recipient: { kind: "address", address: "" },
    valid_days: [true, true, true, true, true, true, true],
    from_time: "00:00",
    to_time: "23:59:59.99",
    transitions: [true, true, true],
    confirmed: true,
    process_identifier: 1,
  };
}

// BACnetDaysOfWeek bit 0 is Monday.
const TRANSITION_NAMES = ["to-offnormal", "to-fault", "to-normal"];

function describeDays(days: boolean[]): string {
  if (days.every(Boolean)) return "every day";
  const on = days.flatMap((d, i) => (d ? [i] : []));
  if (on.length === 0) return "no days";
  const contiguous = on.every((d, i) => i === 0 || d === on[i - 1] + 1);
  if (contiguous && on.length > 2) return `${WEEKDAY_SHORT[on[0]]}–${WEEKDAY_SHORT[on[on.length - 1]]}`;
  return on.map((d) => WEEKDAY_SHORT[d]).join(", ");
}

function describeWindow(from: string, to: string): string {
  if (/^00:00(:00(\.00)?)?$/.test(from) && to.startsWith("23:59")) return "all day";
  return `${from.slice(0, 5)}–${to.slice(0, 5)} device time`;
}

function describeTransitions(transitions: boolean[]): string {
  if (transitions.every(Boolean)) return "all transitions";
  const on = TRANSITION_NAMES.filter((_, i) => transitions[i]);
  return on.length ? on.join(", ") : "no transitions";
}

/** The fields the editor doesn't expose (a BACnet client can set them via
 * AddListElement), shown read-only so a wire-registered entry is visible. */
export function recipientSummary(r: NotificationRecipientEntry, keepPartsWhole = false): string {
  const parts = [
    `pid ${r.process_identifier}`,
    describeDays(r.valid_days),
    describeWindow(r.from_time, r.to_time),
    describeTransitions(r.transitions),
  ];
  // In the narrow table cell, wrap between parts or after a comma, never
  // inside a phrase like "all transitions".
  return (keepPartsWhole ? parts.map((p) => p.replace(/(?<!,) /g, " ")) : parts).join(" · ");
}

/** A pure, presentational recipient-list editor (no API/router
 * dependency) so it can be unit-tested directly, same shape as
 * AlarmingTab.tsx's exported AlarmFieldsEditor. */
export function RecipientListEditor({
  recipients,
  onChange,
}: {
  recipients: NotificationRecipientEntry[];
  onChange: (next: NotificationRecipientEntry[]) => void;
}) {
  return (
    <>
      <table className="bp-table" aria-label="Recipients">
        <tbody>
          {recipients.map((r, i) => (
            <tr key={i}>
              <td>
                {/* bp-select's 100% width lets an auto table squeeze it until the option is cut. */}
                <select
                  className="bp-select"
                  style={{ width: "auto" }}
                  aria-label={`Recipient ${i + 1} kind`}
                  value={r.recipient.kind}
                  onChange={(e) => {
                    const kind = e.target.value as "device" | "address";
                    const next = [...recipients];
                    next[i] = {
                      ...r,
                      recipient:
                        kind === "device"
                          ? { kind: "device", instance_number: 0 }
                          : { kind: "address", address: "" },
                    };
                    onChange(next);
                  }}
                >
                  <option value="address">Address</option>
                  <option value="device">Device instance</option>
                </select>
              </td>
              <td>
                {r.recipient.kind === "address" ? (
                  <input
                    className="bp-input bp-input--mono"
                    aria-label={`Recipient ${i + 1} address`}
                    placeholder="192.168.1.200[:47808]"
                    value={r.recipient.address}
                    onChange={(e) => {
                      const next = [...recipients];
                      next[i] = { ...r, recipient: { kind: "address", address: e.target.value } };
                      onChange(next);
                    }}
                  />
                ) : (
                  <input
                    className="bp-input"
                    type="number"
                    aria-label={`Recipient ${i + 1} device instance`}
                    value={r.recipient.instance_number}
                    onChange={(e) => {
                      const next = [...recipients];
                      next[i] = {
                        ...r,
                        recipient: { kind: "device", instance_number: Number(e.target.value) },
                      };
                      onChange(next);
                    }}
                  />
                )}
              </td>
              <td>
                <label className="bp-check">
                  <input
                    type="checkbox"
                    checked={r.confirmed}
                    onChange={(e) => {
                      const next = [...recipients];
                      next[i] = { ...r, confirmed: e.target.checked };
                      onChange(next);
                    }}
                  />
                  <span>Confirmed</span>
                </label>
              </td>
              <td className="bp-muted">{recipientSummary(r, true)}</td>
              <td>
                <Button variant="delete" small type="button" onClick={() => onChange(recipients.filter((_, j) => j !== i))}>
                  Delete
                </Button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <Button small type="button" onClick={() => onChange([...recipients, emptyRecipient()])}>
        Add recipient
      </Button>
    </>
  );
}

type NotificationDraft = {
  recipients: NotificationRecipientEntry[];
  priorityText: string[];
  ackRequired: Transitions;
};

function savedDraft(obj: BacnetObjectDTO): NotificationDraft {
  return {
    recipients: obj.notification_recipient_list ?? [],
    priorityText: (obj.notification_priority ?? DEFAULT_PRIORITY).map(String),
    ackRequired: transitionsOrDefault(obj.notification_ack_required),
  };
}

export default function NotificationClassTab({
  deviceId,
  obj,
}: {
  deviceId: number;
  obj: BacnetObjectDTO;
}) {
  const queryClient = useQueryClient();

  // The draft and what the server last confirmed (the object, or what a save
  // just sent): a refetch refreshes only what wasn't edited since, so a
  // recipient a client adds later still shows up. Priorities are text until
  // saved, so a half-typed or emptied field isn't turned into a number.
  const { form: draft, setForm: setDraft, baseline, setBaseline } = useDraft(obj, savedDraft);
  const currentRecipients = draft.recipients;
  const shownPriority = draft.priorityText;
  const shownAck = draft.ackRequired;
  const setRecipients = (recipients: NotificationRecipientEntry[]) => setDraft((d) => ({ ...d, recipients }));
  const setPriorityText = (priorityText: string[]) => setDraft((d) => ({ ...d, priorityText }));
  const setAckRequired = (ackRequired: Transitions) => setDraft((d) => ({ ...d, ackRequired }));

  const save = useMutation({
    mutationFn: ({ patch }: { patch: Record<string, unknown>; sent: NotificationDraft }) =>
      objectsApi.update(deviceId, obj.id, patch),
    onSuccess: (_result, { sent }) => {
      setBaseline(sent);
      queryClient.invalidateQueries({ queryKey: ["objects", deviceId] });
    },
  });

  const savedPriority = baseline.priorityText.map(Number);
  const savedAck = baseline.ackRequired;
  const priorityProblems = shownPriority.map(priorityProblem);
  const submit = useSubmitProblem(priorityProblems.find((p) => p !== null) ?? null);

  const dirty = differs(shownPriority.map(Number), savedPriority) || differs(shownAck, savedAck) || differs(currentRecipients, baseline.recipients);
  useUnsavedChanges(dirty, `/devices/${deviceId}/objects/${obj.id}`);

  return (
    <div>
      <Form
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          if (!submit.canSubmit()) return;
          // Only what was edited here: a client may have added a recipient
          // with AddListElement, or written Priority, since the tab loaded.
          save.mutate({
            patch: changedFields(
              {
                notification_recipient_list: currentRecipients,
                notification_priority: shownPriority.map(Number),
                notification_ack_required: shownAck,
              },
              {
                notification_recipient_list: baseline.recipients,
                notification_priority: savedPriority,
                notification_ack_required: savedAck,
              }
            ),
            sent: draft,
          });
        }}
      >
        <Fieldset legend="Routing">
          <FormGrid>
            {PRIORITY_LABELS.map((label, slot) => (
              <Field
                key={label}
                label={label}
                error={submit.shown !== null ? priorityProblems[slot] : null}
              >
                <input
                  id={`notification_priority_${slot}`}
                  type="number"
                  min={0}
                  max={255}
                  required
                  value={shownPriority[slot]}
                  onChange={(e) => {
                    const next = [...shownPriority];
                    next[slot] = e.target.value;
                    setPriorityText(next);
                  }}
                />
              </Field>
            ))}
          </FormGrid>
          <span className="bp-help">
            0 is the highest priority, 255 the lowest.
          </span>
          <TransitionChecks
            legend="Ack required"

            idPrefix="notification_ack_required"
            value={shownAck}
            onChange={setAckRequired}
          />
        </Fieldset>
        <h2>Recipients</h2>
        <RecipientListEditor recipients={currentRecipients} onChange={setRecipients} />
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
