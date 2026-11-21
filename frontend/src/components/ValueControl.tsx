import type { ValueFamily } from "../utils/objectTypes";
import { stateOptionLabel } from "../utils/stateNames";

// One point value in its stored text form: a number (analog), "inactive" or
// "active" (binary), a 1-based state number (multi-state). `emptyLabel` adds
// a choice that stores "" (no value, or the schedule's default).
export default function ValueControl({
  id,
  family,
  value,
  onChange,
  states = [],
  emptyLabel,
  required,
  disabled,
  "aria-label": ariaLabel,
  "aria-describedby": describedBy,
  "aria-invalid": invalid,
}: {
  id?: string;
  family: ValueFamily;
  value: string;
  onChange: (value: string) => void;
  states?: string[];
  emptyLabel?: string;
  required?: boolean;
  disabled?: boolean;
  "aria-label"?: string;
  "aria-describedby"?: string;
  "aria-invalid"?: boolean | "true" | "false";
}) {
  const shared = { id, disabled, required, "aria-label": ariaLabel, "aria-describedby": describedBy, "aria-invalid": invalid };
  if (family === "analog") {
    return (
      <input
        {...shared}
        className="bp-input"
        type="number"
        step="any"
        placeholder={emptyLabel}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }
  const options =
    family === "binary"
      ? [
          { value: "inactive", label: "Inactive" },
          { value: "active", label: "Active" },
        ]
      : states.map((name, i) => ({ value: String(i + 1), label: stateOptionLabel(i + 1, name) }));
  const listed = value === "" ? emptyLabel !== undefined : options.some((o) => o.value === value);
  return (
    <select {...shared} className="bp-select" value={value} onChange={(e) => onChange(e.target.value)}>
      {emptyLabel !== undefined && <option value="">{emptyLabel}</option>}
      {!listed && (
        <option value={value} disabled={value === ""}>
          {value === "" ? "Choose a value" : `${value} · not a ${family === "binary" ? "binary value" : "state"}`}
        </option>
      )}
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}
