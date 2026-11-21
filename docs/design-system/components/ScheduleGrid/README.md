The weekly schedule as one glanceable grid: a row per day on a 24-hour axis, a day editor beneath it, and copy-to-days.

**Markup.** `div.bp-sched[role="group"]` > `div.bp-sched__axis` (hour ticks 00, 06, 12, 18, 24 in `bp-sched__ticks`) and one `div.bp-sched__row` per day: `button.bp-sched__day[aria-pressed]` (selects the day being edited; the selected row also has `aria-current="true"`), `div.bp-sched__track` holding `span.bp-sched__seg` blocks positioned with `style="--start:25%;--span:50%"` (fractions of 24 hours), and `span.bp-sched__sum` with the same entries as text. An optional `span.bp-sched__now` with `--at` marks the device's current time on today's row. Editor: `div.bp-card.bp-sched__edit` of `div.bp-sched__entry` rows (`input[type="time"]`, a value `select`, quiet Delete), "Add entry", and `bp-daypick` toggle buttons with "Weekdays", "All days" and a primary "Copy".

**The consumer provides** the entries per day (time and value), the value names, the device clock for the now marker, and the copy action.

- Times are device time, in 24-hour `HH:MM`; label the grid "Device time" and match the clock on the Process tab. Don't mix in browser-local time.
- A value runs from its entry's time until the next entry; the last entry runs to 24:00, and before the first entry the schedule's default value applies. Draw only non-default spans; the empty track is the default, and the summary says so ("Unoccupied all day (default)").
- Every block carries its value name as text. The default block style (accent) is for the main "on" value; `--b` (warn) and `--c` (ok) separate a second and third value.  A fourth and later value reuses `--c`; the label in the block and the summary text are what tell values apart, and a block too narrow for its label shows it in the summary only.  Color is secondary to the label.
- Multi-state schedules store state numbers and the schedule has no state names of its own, so the value is a select of numbers shown as "State 1", "State 2" and so on.  Do not guess names from the objects the schedule writes to; they can disagree or live on other devices.  Binary schedules show "Inactive" and "Active".  Analog schedules use a number input with the unit beside it.
- There is no "relinquish" (null) entry; do not offer one.
- "Copy Monday to" copies the selected day to the days toggled on, and says it replaces their entries. Weekdays and All days are shortcuts that set the toggles; Copy applies them.
- At phone width the day buttons narrow and the text summary drops under each track; the editor stays one column.
- Exceptions use the same value rules, and the priority field is labelled "Priority (1 highest, 16 lowest)".
- Don't: stack seven separate entry forms, show a day's times only in a form, or rely on color to tell values apart.
