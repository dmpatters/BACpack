A small rounded label for process, connector and log-level state.

**Markup.** `span.bp-pill` plus a variant: `--ok`, `--warn`, `--danger`, `--info`; none for neutral (stopped, DEBUG). Add `--plain` to drop the leading dot (log levels, capability tags).

**The consumer provides** the state word. The word is mandatory: color is never the only signal.

- Process and connector states: `running` and `ok` are ok; `stopped` is neutral; `failing (N)` and `crashed` are danger; `proxy` is info.  Transitional process states (`starting`, `stopping`, `reinitializing`) are neutral, written with a trailing ellipsis ("starting…"), and announced with `aria-live="polite"`; they are quiet because they resolve on their own.  A device that stays in one for long ends in `running`, `stopped` or `crashed`, which are the states that get color.  Live-apply states: `restart pending` is warn, `apply failed` is danger; `applying` is neutral with an ellipsis.
- Log levels: DEBUG neutral, INFO info, WARNING warn, ERROR danger, always `--plain`, uppercase as the logger writes them.
- Keep to one or two words and a short count in parentheses. Don't: use a pill for a button, a count badge or decoration.
