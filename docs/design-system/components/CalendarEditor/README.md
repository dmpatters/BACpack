The editor for a Calendar object's date list: entries written in plain language, added through a form that adapts to the kind of entry.

**Markup.** List the entries in a `DataTable` with columns Kind (plain `StatusPill`), Matches (plain language), BACnet entry (`bp-cell-id`, the raw form for people who know it), and row actions. Add and edit use a `bp-fieldset` with `div.bp-segmented[role="radiogroup"]` (labels wrapping `input[type="radio"]` + `span`) for the kind, a `bp-form-grid` of fields for that kind, and `div.bp-match` showing what the entry means. Today's status sits above the table as a `StatusPill`.

**The consumer provides** the entries, the field values for the chosen kind, the plain-language summary, and the next three matching dates.

- Three kinds: **Date** (year, month, day, with "Any" and "Last" as named options), **Date range** (start and end date pickers), **Pattern** (month, which week, weekday). Show only the chosen kind's fields.
- Wildcards are words in dropdowns ("Every month", "Every day", "Last"), never `*` or `last` typed into a text box.
- The summary reads like a sentence ("Last Friday of every month", "December 25, every year") and updates as the fields change; below it, "Next:" lists the next three matching dates so the person can check the rule.
- Keep the raw BACnet form (`month * · week last · weekday 5`) visible, muted and in mono, in the entries table; never use it as the only description.
- Show whether today is in the calendar as a pill above the list ("today is in calendar" is ok, "not in calendar" is neutral), using the same date as the device clock, in device time.
- Name the tab "Dates", not "Schedule".  Calendars have no tab of their own today, so Dates is new and replaces whatever raw date-list field the General tab has now.  Calendars keep the Trending tab like every other object type.
- Wildcards are limited to what BACpack supports: "Any" and "Last" (week of month).  BACnet's odd and even months and days are left out on purpose; do not add them to the dropdowns.
- Don't: use free-text fields for wildcards, show cryptic summaries, or let an entry that matches nothing be saved without a warning.
