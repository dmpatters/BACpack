import { useEffect, useId, useRef, useState } from "react";
import Button from "./Button";
import Callout from "./Callout";
import { useConfirm } from "./ConfirmDialog";
import { Fieldset } from "./Form";
import {
  MAX_STATES,
  STATE_PRESETS,
  addState,
  deleteState,
  moveState,
  presetStates,
  referenceText,
  renameState,
  stateCountText,
  stateProblems,
  trackOrigins,
  type StateEdit,
  type StateReference,
} from "../utils/stateNames";

// docs/design-system/components/StateEditor. `savedNames` is the numbering
// the references point at; each row remembers which saved state it was, so a
// moved state is still asked about by what it was.
export default function StateEditor({
  names,
  savedNames,
  references = [],
  onChange,
  legend = "States",
}: {
  names: string[];
  savedNames: string[];
  references?: StateReference[];
  onChange: (edit: StateEdit) => void;
  legend?: string;
}) {
  const errorId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const focusNext = useRef<string | null>(null);
  const { ask, dialog } = useConfirm();
  const [origins, setOrigins] = useState<(number | null)[]>(() =>
    names.map((_, i) => (i < savedNames.length ? i + 1 : null))
  );

  // A save makes the sent list the numbering everything refers to. Keyed on
  // the content: a refetch hands over an equal but new array, which must not
  // forget where moved rows came from.
  const savedKey = JSON.stringify(savedNames);
  useEffect(() => {
    setOrigins(savedNames.map((_, i) => i + 1));
  }, [savedKey]);

  // Focus follows the edited row once the new list has rendered.
  useEffect(() => {
    const label = focusNext.current;
    if (!label) return;
    focusNext.current = null;
    rootRef.current?.querySelector<HTMLElement>(`[aria-label="${label}"]`)?.focus();
  });

  const apply = (edit: StateEdit) => {
    setOrigins((current) => trackOrigins(current, edit));
    onChange(edit);
  };

  const move = (index: number, delta: -1 | 1) => {
    const to = index + delta;
    const atEnd = to === 0 || to === names.length - 1;
    const direction = atEnd ? (delta === -1 ? "down" : "up") : delta === -1 ? "up" : "down";
    focusNext.current = `Move state ${to + 1} ${direction}`;
    apply(moveState(names, index, delta));
  };

  const remove = (index: number) => {
    const doDelete = () => {
      focusNext.current = names.length > 1 ? `Name of state ${Math.min(index + 1, names.length - 1)}` : null;
      apply(deleteState(names, index));
    };
    const origin = origins[index] ?? null;
    const users = origin === null ? [] : references.filter((r) => r.states.includes(origin));
    if (users.length === 0) {
      doDelete();
      return;
    }
    const name = names[index]?.trim();
    ask({
      title: `Delete state ${index + 1}${name ? ` (${name})` : ""}?`,
      body: users.map((u) => referenceText(u, savedNames)).join(" "),
      confirmLabel: "Delete state",
      onConfirm: doDelete,
    });
  };

  const usersOf = (dropped: (number | null)[]) =>
    references.filter((r) => r.states.some((s) => dropped.includes(s)));

  const fillPreset = (preset: { label: string; names: string[] }) => {
    const edit = presetStates(names, preset.names);
    const users = usersOf(origins.filter((_, i) => edit.renumber[i] === null));
    if (users.length === 0) {
      apply(edit);
      return;
    }
    ask({
      title: `Replace the states with ${preset.label}?`,
      body: users.map((u) => referenceText(u, savedNames)).join(" "),
      confirmLabel: "Replace states",
      onConfirm: () => apply(edit),
    });
  };

  const problems = stateProblems(names);

  return (
    <Fieldset legend={legend}>
      <div className="bp-stack" ref={rootRef}>
        <div className="bp-presets">
          <span className="bp-label">Start from</span>
          {STATE_PRESETS.map((preset) => (
            <Button key={preset.label} small type="button" onClick={() => fillPreset(preset)}>
              {preset.label}
            </Button>
          ))}
        </div>
        <ol className="bp-states" aria-label="State names">
          {names.map((name, i) => {
            const n = i + 1;
            const problem = problems.rows.has(i);
            return (
              <li key={i} className={problem ? "bp-state bp-state--error" : "bp-state"}>
                <span className="bp-state__n">{n}</span>
                <input
                  className="bp-input"
                  type="text"
                  value={name}
                  aria-label={`Name of state ${n}`}
                  aria-invalid={problem ? true : undefined}
                  aria-describedby={problem ? `${errorId}-${i}` : undefined}
                  onChange={(e) => apply(renameState(names, i, e.target.value))}
                />
                <span className="bp-state__tools">
                  <Button
                    variant="ghost"
                    small
                    type="button"
                    aria-label={`Move state ${n} up`}
                    disabled={i === 0}
                    onClick={() => move(i, -1)}
                  >
                    ↑
                  </Button>
                  <Button
                    variant="ghost"
                    small
                    type="button"
                    aria-label={`Move state ${n} down`}
                    disabled={i === names.length - 1}
                    onClick={() => move(i, 1)}
                  >
                    ↓
                  </Button>
                  <Button
                    variant="delete"
                    small
                    type="button"
                    aria-label={`Delete state ${n}`}
                    disabled={names.length === 1}
                    onClick={() => remove(i)}
                  >
                    Delete
                  </Button>
                </span>
              </li>
            );
          })}
        </ol>
        {problems.message && (
          <p className="bp-states__error" role="alert">
            {names.length === 0
              ? problems.message
              : [...problems.rows].map(([i, message]) => (
                  <span key={i} id={`${errorId}-${i}`}>
                    {message}{" "}
                  </span>
                ))}
          </p>
        )}
        <div className="bp-states__footer">
          <Button
            small
            type="button"
            disabled={names.length >= MAX_STATES}
            onClick={() => {
              focusNext.current = `Name of state ${names.length + 1}`;
              apply(addState(names));
            }}
          >
            Add state
          </Button>
          <span className="bp-states__count">{stateCountText(names.length)}</span>
        </div>
        {references.length > 0 && (
          <Callout tone="warn" lead="Reordering renumbers states.">
            {references.map((r) => referenceText(r, savedNames)).join(" ")} Update{" "}
            {references.length === 1 ? "it" : "them"} after moving or deleting states.
          </Callout>
        )}
      </div>
      {dialog}
    </Fieldset>
  );
}
