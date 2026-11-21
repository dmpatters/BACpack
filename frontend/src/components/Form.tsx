import {
  cloneElement,
  useId,
  useState,
  type FormHTMLAttributes,
  type InputHTMLAttributes,
  type ReactElement,
  type ReactNode,
} from "react";

// A form problem shown only after a submit, then kept live: it goes away
// once the form is fixed, not at the next submit.
export function useSubmitProblem(problem: string | null) {
  const [tried, setTried] = useState(false);
  return {
    shown: tried ? problem : null,
    canSubmit: () => {
      setTried(problem !== null);
      return problem === null;
    },
  };
}

// useSubmitProblem for a form whose rules belong to different fields: the
// first failing rule shows under its own field.
export function useFieldProblem<F extends string>(rules: [F, string | false][]) {
  const first = rules.find(([, message]) => message !== false);
  const submit = useSubmitProblem(first ? (first[1] as string) : null);
  return {
    canSubmit: submit.canSubmit,
    errorFor: (field: F) => (first && first[0] === field ? submit.shown : null),
  };
}

export function Form({ className, ...props }: FormHTMLAttributes<HTMLFormElement>) {
  return <form {...props} className={className ? `bp-form ${className}` : "bp-form"} />;
}

export function Fieldset({
  legend,
  children,
  disabled,
  hidden,
  id,
}: {
  legend: ReactNode;
  children: ReactNode;
  disabled?: boolean;
  hidden?: boolean;
  id?: string;
}) {
  return (
    <fieldset className="bp-fieldset" disabled={disabled} hidden={hidden} id={id}>
      <legend>{legend}</legend>
      {children}
    </fieldset>
  );
}

export function FormGrid({ children }: { children: ReactNode }) {
  return <div className="bp-form-grid">{children}</div>;
}

// A settings page's panels stacked in one centred form-width column. The page
// header, intro text and any table stay outside it, at full width.
export function SettingsColumn({ children }: { children: ReactNode }) {
  return <div className="bp-settings">{children}</div>;
}

// Two groups side by side on a wide screen, one above the other below 1100px.
// Each child is one column; a form holding it needs `bp-form--wide`.
export function Columns({ children }: { children: ReactNode }) {
  return <div className="bp-columns">{children}</div>;
}

export function FormActions({ children }: { children: ReactNode }) {
  return <div className="bp-form-actions">{children}</div>;
}

type ControlProps = {
  id?: string;
  type?: string;
  className?: string;
  "aria-describedby"?: string;
  "aria-invalid"?: boolean | "true" | "false";
};

// A plain input, select or textarea gets the design-system class; a checkbox,
// a radio or a component (Combobox) brings its own look.
function controlClass(control: ReactElement<ControlProps>): string | undefined {
  const own = control.props.className;
  const tokens = (own ?? "").split(/\s+/);
  if (typeof control.type !== "string" || tokens.includes("bp-input") || tokens.includes("bp-select")) return own;
  if (control.props.type === "checkbox" || control.props.type === "radio") return own;
  return [control.type === "select" ? "bp-select" : "bp-input", own].filter(Boolean).join(" ");
}

function joinIds(...ids: (string | undefined)[]): string | undefined {
  return ids.filter(Boolean).join(" ") || undefined;
}

// One control with its visible label, and one line of help or the error that
// replaces it. The control is the only child; it keeps its own id if it has one.
export function Field({
  label,
  help,
  error,
  id,
  children,
}: {
  label: ReactNode;
  help?: ReactNode;
  error?: string | null;
  id?: string;
  children: ReactElement<ControlProps>;
}) {
  const autoId = useId();
  const controlId = id ?? children.props.id ?? autoId;
  const noteId = `${controlId}-note`;
  const control = cloneElement(children, {
    id: controlId,
    className: controlClass(children),
    "aria-describedby": joinIds(children.props["aria-describedby"], error || help ? noteId : undefined),
    "aria-invalid": error ? true : children.props["aria-invalid"],
  });
  return (
    <div className={error ? "bp-field bp-field--error" : "bp-field"}>
      <label className="bp-label" htmlFor={controlId}>
        {label}
      </label>
      {control}
      {error ? (
        <span className="bp-field-error" id={noteId} role="alert">
          {error}
        </span>
      ) : help ? (
        <span className="bp-help" id={noteId}>
          {help}
        </span>
      ) : null}
    </div>
  );
}

export function CheckField({
  label,
  help,
  ...input
}: { label: ReactNode; help?: ReactNode } & Omit<InputHTMLAttributes<HTMLInputElement>, "type">) {
  const helpId = useId();
  return (
    <div className="bp-field">
      <label className="bp-check">
        <input type="checkbox" {...input} aria-describedby={joinIds(input["aria-describedby"], help ? helpId : undefined)} />
        <span>{label}</span>
      </label>
      {help && (
        <span className="bp-help" id={helpId}>
          {help}
        </span>
      )}
    </div>
  );
}

// docs/design-system/components/Form: a choice of two to four, shown at once.
export function Segmented<V extends string>({
  label,
  value,
  options,
  onChange,
  disabled,
}: {
  label: string;
  value: V;
  options: { value: V; label: string }[];
  onChange: (value: V) => void;
  disabled?: boolean;
}) {
  const name = useId();
  return (
    <div className="bp-segmented" role="radiogroup" aria-label={label}>
      {options.map((option) => (
        <label key={option.value}>
          <input
            type="radio"
            name={name}
            value={option.value}
            checked={value === option.value}
            disabled={disabled}
            onChange={() => onChange(option.value)}
          />
          <span>{option.label}</span>
        </label>
      ))}
    </div>
  );
}
