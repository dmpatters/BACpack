The row above a long table: search, filters, count, and the bulk-action bar that replaces the count when rows are selected.

**Markup.** `div.bp-toolbar` > `input.bp-input.bp-toolbar__search` (`type="search"`), optional `select.bp-select.bp-toolbar__select`, then either `span.bp-toolbar__count` or `div.bp-bulkbar` (`span.bp-bulkbar__count` + small buttons). Sortable columns: `th[aria-sort]` containing `button.bp-sort`; use `ascending`, `descending`, or omit `aria-sort` for an unsorted column.

**The consumer provides** the search handler, filters, counts and the bulk actions.

- Any table over about 15 rows gets a toolbar with search, and sortable headers on name and id columns.
- The toolbar keeps a fixed height. The bulk bar swaps in for the count in the same row, so the table never shifts under the cursor.
- Bulk bar shows "N selected", then Edit actions, then the quiet red Delete, then Clear. Deleting asks for confirmation (`ConfirmDialog`).
- Don't: put the bulk bar above the toolbar or insert it as a new row, or sort on a column without showing `aria-sort`.
