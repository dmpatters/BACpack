Buttons and actions: primary, secondary, ghost, quiet danger, solid danger, small row actions.

**Markup.** `button.bp-btn` plus one modifier: `--primary`, `--ghost`, `--danger` (quiet red), `--danger-solid`, `--delete` (row delete), and `--sm` for table rows and cards. Use `<a class="bp-btn">` for navigation that looks like a button.

**The consumer provides** the label (verb-first, sentence case) and the type (`button` or `submit`).

- One primary button per form or card ("Create device", "Add connector", "Save").
- Secondary (no modifier) for neutral actions: "Restart all running devices", "Stop", "Restart serving".
- Delete is two steps: `--danger` opens a confirmation, `--danger-solid` confirms. Row deletes use `--sm --delete` and stay muted until hover or focus.
- Disabled buttons use the `disabled` attribute; they keep their label.
- Don't: stack several primaries, use a solid red button outside a confirmation, or put an icon-only button without an `aria-label`.
