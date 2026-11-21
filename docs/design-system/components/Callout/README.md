An inline note that explains or warns, with a bold lead-in and one or two sentences.

**Markup.** `div.bp-callout` plus `--info`, `--ok`, `--warn` or `--danger`; lead with `<strong>`. Use `role="note"` for info, `role="status"` for success, `role="alert"` for warn and danger.

**The consumer provides** the lead-in phrase and the text.

- Use `--warn` for anything that deletes or overwrites data, such as a full-sync config import, and place it directly above the action it warns about.
- Use `--info` instead of long gray paragraphs under a page title.
- Don't: stack more than two callouts on a page, put buttons inside, or use a callout for a transient toast.
