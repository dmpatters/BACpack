The dense data table used for objects, connectors, mappings and bindings.

**Markup.** `div.bp-table-wrap` > `table.bp-table`. Cell classes: `bp-cell-check` (checkbox column), `bp-cell-id` (mono, muted ids like `analog-input:1`), `bp-cell-mono`, `bp-cell-num` (right aligned, tabular figures; put it on the `th` too), `bp-cell-clip` (one-line clipped mono text, full value in `title`), `bp-cell-actions` (right aligned row actions). A selected row gets `data-selected` (`aria-selected` is valid only on a grid's rows; the row's checkbox carries the selection for assistive tech).

**The consumer provides** column headers in sentence-length words (they render uppercase), row data, and row actions.

- Order columns: select, id, name, description, units, flags, status, numbers, actions.
- Row actions: an "Edit" link followed by a `bp-btn--sm bp-btn--delete` button. Don't show a solid red button on every row.
- Numbers right-aligned; ids and addresses in mono; empty cells show a muted em dash.
- The header row sticks inside the wrapper when the table scrolls; give long tables a `max-height` on the wrapper.
- Tables over about 15 rows get a `TableToolbar` (search, filter) and sortable name and id headers (`th[aria-sort]` + `button.bp-sort`).
- Keep the border under the actions cell identical to every other cell: same `td`, same border, no custom wrapper.
- Don't: zebra-stripe, wrap long URLs across lines, or put timestamps in a wrapping column (see `LogTable`).
