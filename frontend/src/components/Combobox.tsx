import { Fragment, useEffect, useId, useMemo, useState, type KeyboardEvent } from "react";

export interface ComboOption {
  value: string;
  label: string;
  group: string;
  hint?: string;
}

export function filterOptions(options: ComboOption[], query: string): ComboOption[] {
  const q = query.trim().toLowerCase();
  if (!q) return options;
  return options.filter((o) => o.label.toLowerCase().includes(q) || o.value.toLowerCase().includes(q));
}

function Highlight({ text, query }: { text: string; query: string }) {
  const q = query.trim();
  const at = q ? text.toLowerCase().indexOf(q.toLowerCase()) : -1;
  if (at < 0) return <>{text}</>;
  return (
    <>
      {text.slice(0, at)}
      <mark>{text.slice(at, at + q.length)}</mark>
      {text.slice(at + q.length)}
    </>
  );
}

// docs/design-system/components/Combobox. Only a listed option can be
// chosen: typed text filters the list and is dropped when the field closes.
export default function Combobox({
  id,
  options,
  value,
  onChange,
  emptyText,
  disabled,
  placeholder,
  "aria-describedby": describedBy,
  "aria-invalid": invalid,
}: {
  id?: string;
  options: ComboOption[];
  value: string;
  onChange: (value: string) => void;
  emptyText: (query: string) => string;
  disabled?: boolean;
  placeholder?: string;
  "aria-describedby"?: string;
  "aria-invalid"?: boolean;
}) {
  const listId = useId();
  const optionId = (i: number) => `${listId}-option-${i}`;
  const [query, setQuery] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const shown = useMemo(() => filterOptions(options, query ?? ""), [options, query]);
  const chosen = options.find((o) => o.value === value);

  useEffect(() => {
    if (open && active >= 0) document.getElementById(optionId(active))?.scrollIntoView?.({ block: "nearest" });
  });

  const openList = () => {
    setOpen(true);
    setActive(Math.max(0, shown.findIndex((o) => o.value === value)));
  };
  const close = () => {
    setOpen(false);
    setQuery(null);
    setActive(-1);
  };
  const choose = (option: ComboOption) => {
    onChange(option.value);
    close();
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!open) return openList();
      if (shown.length === 0) return;
      const step = e.key === "ArrowDown" ? 1 : -1;
      setActive((i) => (i + step + shown.length) % shown.length);
    } else if (e.key === "Enter" && open) {
      e.preventDefault();
      if (active >= 0 && shown[active]) choose(shown[active]);
    } else if (e.key === "Escape" && open) {
      e.preventDefault();
      e.stopPropagation();
      close();
    }
  };

  const listShown = open && shown.length > 0;
  let lastGroup = "";
  return (
    <div className="bp-combo">
      <input
        id={id}
        className="bp-input"
        role="combobox"
        type="text"
        autoComplete="off"
        aria-expanded={open}
        aria-controls={listShown ? listId : undefined}
        aria-autocomplete="list"
        aria-activedescendant={open && active >= 0 && shown[active] ? optionId(active) : undefined}
        aria-describedby={describedBy}
        aria-invalid={invalid}
        disabled={disabled}
        // An empty value shows as an empty field (with the empty option's label
        // as placeholder), so typing filters instead of appending to "None".
        placeholder={placeholder ?? (value ? undefined : chosen?.label)}
        value={query ?? (value ? (chosen?.label ?? value) : "")}
        onChange={(e) => {
          setQuery(e.target.value);
          setOpen(true);
          setActive(0);
        }}
        // A click opens the list without an active option; the arrows start
        // from the top.
        onClick={() => {
          if (!open) {
            setOpen(true);
            setActive(-1);
          }
        }}
        onKeyDown={onKeyDown}
        onBlur={close}
      />
      {listShown && (
        // mousedown would blur the input (closing the list) before the click lands.
        <ul className="bp-combo__list" role="listbox" id={listId} onMouseDown={(e) => e.preventDefault()}>
          {shown.map((option, i) => {
            const heading = option.group && option.group !== lastGroup ? option.group : null;
            lastGroup = option.group;
            return (
              <Fragment key={option.value}>
                {heading && (
                  <li className="bp-combo__group" role="presentation">
                    {heading}
                  </li>
                )}
                <li
                  id={optionId(i)}
                  className="bp-combo__option"
                  role="option"
                  aria-selected={option.value === value}
                  data-active={i === active ? "" : undefined}
                  onClick={() => choose(option)}
                >
                  <span>
                    <Highlight text={option.label} query={query ?? ""} />
                  </span>
                  {option.hint && (
                    <span className="bp-combo__hint">
                      <Highlight text={option.hint} query={query ?? ""} />
                    </span>
                  )}
                </li>
              </Fragment>
            );
          })}
        </ul>
      )}
      {open && shown.length === 0 && (
        <div className="bp-combo__empty" role="status">
          {emptyText((query ?? "").trim())}
        </div>
      )}
    </div>
  );
}
