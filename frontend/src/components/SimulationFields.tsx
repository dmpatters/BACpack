import { fieldLabel, fieldsFor, type SimFamily, type SimGroup, type SimValues } from "../utils/simulation";
import { stateOptionLabel } from "../utils/stateNames";
import Combobox, { type ComboOption } from "./Combobox";
import { CheckField, Field, Fieldset, FormGrid } from "./Form";

const GROUPS: SimGroup[] = ["Signal", "Timing", "Output"];

// The settings of one simulator kind, in the design's groups; a group the
// kind has no fields for isn't shown. Shared by the Simulation tab and the
// bulk "Simulate selected" panel (which has no unit, sources or state names).
export default function SimulationFields({
  family,
  kind,
  values,
  onChange,
  disabled = false,
  idPrefix,
  unit = "",
  errors = {},
  sources,
  stateNames,
}: {
  family: SimFamily;
  kind: string;
  values: SimValues;
  onChange: (values: SimValues) => void;
  disabled?: boolean;
  idPrefix: string;
  unit?: string;
  errors?: Record<string, string>;
  sources?: ComboOption[];
  stateNames?: string[];
}) {
  const fields = fieldsFor(family, kind);
  const set = (key: string, value: string) => onChange({ ...values, [key]: value });
  const statesField = fields.find((f) => f.type === "states");

  return (
    <>
      {GROUPS.map((group) => {
        const inGroup = fields.filter((f) => f.group === group && f.type !== "states");
        if (inGroup.length === 0) return null;
        return (
          <Fieldset key={group} legend={group} disabled={disabled}>
            <FormGrid>
              {inGroup.map((field) => {
                const id = `${idPrefix}_${field.key}`;
                const label = fieldLabel(field, unit);
                if (field.type === "source" && sources) {
                  return (
                    <Field key={field.key} label={label} help={field.help} error={errors[field.key]}>
                      <Combobox
                        id={id}
                        options={sources}
                        value={values.source ?? ""}
                        onChange={(v) => set("source", v)}
                        emptyText={(query) => `No analog object matches "${query}".`}
                        disabled={disabled}
                      />
                    </Field>
                  );
                }
                return (
                  <Field key={field.key} label={label} help={field.help} error={errors[field.key]}>
                    <input
                      id={id}
                      type={field.type === "number" ? "number" : "text"}
                      step="any"
                      required={!field.optional}
                      placeholder={field.type === "source" ? "analog-value:3" : field.placeholder}
                      value={values[field.key] ?? ""}
                      onChange={(e) => set(field.key, e.target.value)}
                    />
                  </Field>
                );
              })}
            </FormGrid>
          </Fieldset>
        );
      })}
      {statesField && (
        <Fieldset legend="States" disabled={disabled}>
          {stateNames ? (
            <StatesChecklist
              kind={kind}
              names={stateNames}
              value={values.states ?? ""}
              error={errors.states}
              onChange={(v) => set("states", v)}
            />
          ) : (
            <Field label="State numbers" help="Separated by commas; blank uses every state." error={errors.states}>
              <input id={`${idPrefix}_states`} type="text" value={values.states ?? ""} onChange={(e) => set("states", e.target.value)} />
            </Field>
          )}
        </Fieldset>
      )}
    </>
  );
}

// Blank means every state. The last two checked can't be unchecked: the
// simulator needs at least two.
function StatesChecklist({
  kind,
  names,
  value,
  error,
  onChange,
}: {
  kind: string;
  names: string[];
  value: string;
  error?: string;
  onChange: (value: string) => void;
}) {
  const chosen = value.split(/[\s,;]+/).filter(Boolean).map(Number);
  const all = chosen.length === 0;
  const isOn = (n: number) => all || chosen.includes(n);
  const checkedCount = names.filter((_, i) => isOn(i + 1)).length;
  const toggle = (n: number, on: boolean) => {
    // A stored state above the object's count isn't listed, so it goes here.
    const current = all ? names.map((_, i) => i + 1) : chosen.filter((s) => s <= names.length);
    const next = (on ? [...current, n] : current.filter((s) => s !== n)).sort((a, b) => a - b);
    const everything = names.every((_, i) => next.includes(i + 1));
    onChange(everything ? "" : next.join(", "));
  };
  return (
    <div className="bp-stack">
      {names.map((name, i) => (
        <CheckField
          key={i}
          label={stateOptionLabel(i + 1, name)}
          checked={isOn(i + 1)}
          disabled={isOn(i + 1) && checkedCount <= 2}
          onChange={(e) => toggle(i + 1, e.target.checked)}
        />
      ))}
      {error ? (
        <span className="bp-field-error" role="alert">
          {error}
        </span>
      ) : (
        <span className="bp-help">
          Leave all checked to use every state. Check at least two.
        </span>
      )}
    </div>
  );
}
