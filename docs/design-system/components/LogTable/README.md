The log and traffic view: filter row above a table whose rows each stay on one line.

**Markup.** `div.bp-log-filters` of `div.bp-field` selects, then `div.bp-table-wrap` > `table.bp-table`. Cells: `bp-cell-time` (timestamp), a plain `StatusPill` for level, `bp-cell-id` for device and logger, `bp-cell-msg` for the message.

**The consumer provides** rows newest first, the filters, and the refresh (the list refreshes on its own every few seconds).

- Timestamp format `YYYY-MM-DD HH:MM:SS`, one line, `Time (UTC)` in the header.
- The message column takes the remaining width and may wrap; every other column is `nowrap`.
- Traffic uses the same table with Time, Event (mono) and Detail columns.
- Don't: show the zone in every cell, wrap timestamps, or tint whole rows by level (the pill carries it).
