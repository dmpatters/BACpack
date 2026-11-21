A text input with a filtered, grouped list: the replacement for any select with more than about 15 options (units has about 250).

**Markup.** `div.bp-combo` > `input.bp-input[role="combobox"]` (`aria-expanded`, `aria-controls` while the list is shown, `aria-autocomplete="list"`) and `ul.bp-combo__list[role="listbox"]` > `li.bp-combo__group` headings and `li.bp-combo__option[role="option"]`. The highlighted option has `data-active`, the chosen one `aria-selected="true"`. Wrap matched text in `<mark>`, in the label and the hint. Show the raw BACnet enum as `span.bp-combo__hint` (mono, muted). Empty result: `div.bp-combo__empty` ("No units match 'xyz'").

**The consumer provides** the options with a friendly label, the enum value, and a group; filtering by label and enum; keyboard handling (up/down, Enter, Escape).

- Show readable labels ("Degrees Fahrenheit") and keep the enum visible as the hint so the value is never hidden.
- Group long lists (Temperature, Pressure, Flow, Electrical) and put frequently used units first.
- Don't: use it for short lists (use `select.bp-select`), or accept free text where the field must be a valid enum. Bulk edit uses the same control as single edit.
