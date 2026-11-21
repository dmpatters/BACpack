A card for one virtual device on the home screen: name, state, meta and a single action.

**Markup.** `div.bp-device-grid` > `article.bp-device` > `div.bp-device__head` (`a.bp-device__name` + `StatusPill`), `dl.bp-device__meta` (device, objects, pid, bind), `div.bp-device__actions`.

**The consumer provides** the device name and link, state, device number, object count, pid (or an em dash when stopped), bind address and the one action: "Stop" when running, "Start" (primary) when stopped.

- Meta keys are lowercase words in mono; values in `text`. Counts are real plurals via the key ("objects" with a number), never "object(s)".
- The grid fills as many 280px-minimum columns as fit.
- Pending work shows as a pill next to the state, never as another button: `restart pending` (warn) or `apply failed` (danger).  "Restart serving", the live-apply Callouts and any error text live on the device page only.
- Don't: add a delete button here (delete lives on the device page), more than one action, or charts.
