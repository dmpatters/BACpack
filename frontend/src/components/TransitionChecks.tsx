import { CheckField, Fieldset } from "./Form";

export type Transitions = [boolean, boolean, boolean];

/** What the device serves for a transitions list that was never set. */
export const ALL_TRANSITIONS: Transitions = [true, true, true];

export function transitionsOrDefault(value: Transitions | null | undefined): Transitions {
  return value ?? ALL_TRANSITIONS;
}

// Stored list order, which is also the BACnet bit order.
const LABELS = ["To off-normal", "To fault", "To normal"];

/** One checkbox per event transition, for Event_Enable and Ack_Required. */
export function TransitionChecks({
  legend,
  help,
  value,
  onChange,
  idPrefix,
}: {
  legend: string;
  help?: string;
  value: Transitions;
  onChange: (next: Transitions) => void;
  idPrefix: string;
}) {
  return (
    <Fieldset legend={legend}>
      {LABELS.map((label, slot) => (
        <CheckField
          key={label}
          id={`${idPrefix}-${slot}`}
          label={label}
          checked={value[slot]}
          onChange={(e) => {
            const next: Transitions = [...value];
            next[slot] = e.target.checked;
            onChange(next);
          }}
        />
      ))}
      {help && <span className="bp-help">{help}</span>}
    </Fieldset>
  );
}
