The editor for the named states of a multi-state object: one list of numbered names replaces the separate "Number of states" field and the comma-joined text box.

**Markup.** `fieldset.bp-fieldset` (legend "States") holding optional `div.bp-presets` (small buttons that fill the list), `ol.bp-states` of `li.bp-state` rows (`span.bp-state__n` number in mono, `input.bp-input` name, `span.bp-state__tools` with move up, move down and `bp-btn--delete`), `p.bp-states__error` for validation, and `div.bp-states__footer` with "Add state" and `span.bp-states__count`. A row with a problem gets `bp-state--error` plus `aria-invalid` and `aria-describedby`. The field that picks a state (relinquish default) is a plain `select.bp-select` listing "number · name".

**The consumer provides** the current states, the presets for the object's kind, which states other objects use, and the save handler.

- The number of states is the length of the list; there is no separate count field. Say it in the footer in words ("4 states", "1 state").
- States are numbered 1 to N in list order, as BACnet does. Names are required and unique; show the error under the list and next to the offending row.
- Choose states by name everywhere (relinquish default, schedule values), never by typing a number. Options read "2 · Low".
- Reordering or deleting renumbers states. When other objects reference a state, show a warn `Callout` naming them ("Schedule OCC_SCHEDULE uses state 3"); deleting an in-use state asks first with `ConfirmDialog`. Renaming is always safe.
- Presets only fill the list; they never save. Offer "Off / On", "Off / Low / High", and "Occupied / Standby / Unoccupied".
- Multi-state objects only.  Binary objects have no state-name properties in BACpack (they are only active and inactive), so they do not get a `StateEditor`; show "Inactive" and "Active" as fixed words wherever a value is displayed.
- Don't: use a comma-separated text field, show a count field that can disagree with the list, or accept an empty name.
