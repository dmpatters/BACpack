Form controls and layout: fields, labels, inputs, selects, checkboxes, fieldset cards and actions.

**Markup.** `form.bp-form` (max `form-max`) > optional `fieldset.bp-fieldset` with a `legend`, `div.bp-form-grid` for rows of up to three fields, `div.bp-field` > `label.bp-label` + `input.bp-input` or `select.bp-select` (+ `span.bp-help`). Checkboxes: `label.bp-check`. Buttons in `div.bp-form-actions`. Use `bp-input--mono` for addresses, URLs and JSON paths.

**The consumer provides** every label (visible, above the field, bound with `for`/`id`), the values and validation messages.

- Related fields share a row in a grid (poll interval, timeout, response format); unrelated groups get their own fieldset.
- The primary action sits left under the last field; a ghost "Cancel" may follow it. Don't stretch inputs across the page: the form column stops at `form-max`.
- On a page made only of forms and facts, put its groups side by side in `div.bp-columns`, one child per column. Each column still stops at `form-max`, and below 1100px the columns stack in source order. A form wrapping the columns takes `bp-form--wide`.
- Help text is one line in `bp-help`. Errors replace the help text in `danger` and name the fix.
- Don't: use placeholders as labels, color the whole field red, or put a label inside the border.
