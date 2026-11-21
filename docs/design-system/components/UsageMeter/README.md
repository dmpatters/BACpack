A horizontal meter for storage use, plus the small in-table bar for each object's share.

**Markup.** `div.bp-meter` (with `role="meter"` and `aria-valuemin`, `aria-valuemax`, `aria-valuenow`) > `div.bp-meter__head` (label left, percent in mono right) and `div.bp-meter__track` > `div.bp-meter__fill` with `style="--value:N%"`. Add `bp-meter--sm` for bars inside table cells. Fill variants: default accent, `--ok`, `--warn`, `--danger`.

**The consumer provides** used and limit in MB, and the percentage.

- Fill `accent` normally, `warn` above about 80% of the limit, `danger` above 95%; the head text always states the numbers.
- Under the meter, list objects sorted largest first in a `DataTable`: object, device, share (inline bar), size right-aligned in mono.
- Don't: use a pie chart with a per-object color legend; with this many series it can't be read and fails for color-blind users.
