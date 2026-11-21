The confirmation step for anything destructive: device delete, bulk object delete, full-sync import.

**Markup.** `div.bp-scrim` (full-screen overlay; `bp-scrim--inline` only for previews) > `div.bp-dialog[role="alertdialog"][aria-modal="true"]` with `h2.bp-dialog__title`, `p.bp-dialog__text`, and `div.bp-dialog__actions`. A native `<dialog class="bp-dialog">` works the same, with `::backdrop` coloured by `scrim`.

**The consumer provides** the title (a question naming the thing and its id), the consequence in one or two sentences with the count of what is lost, and the action label.

- Cancel is a ghost button on the left and takes initial focus; the confirm button is `bp-btn--danger-solid`, labelled with the verb and object ("Delete device"), never "OK".
- This is the only place the solid red button appears. Escape and a click on the scrim cancel.
- Name what else goes with it ("its 55 objects and their trend history") so the person decides with the full cost in view.
- Don't: use it for reversible actions, stack dialogs, or ask twice.
