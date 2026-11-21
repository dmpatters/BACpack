The states between asking for something and getting it: loading, failure, stale data, invalid input, and not found.

**Markup.** Loading: `div.bp-loading[role="status"]` with `span.bp-spinner` and text, or `div.bp-skeleton-rows` of `div.bp-skeleton` bars shaped like the content. Failure: `div.bp-error[role="alert"]` with a bold lead-in, the reason, and a Retry button. Stale: `div.bp-result.bp-result--stale` with a "previous result" warn pill and its time. Field: `div.bp-field--error` with `span.bp-field-error`, `aria-invalid` and `aria-describedby`. Not found: `div.bp-empty` with `h4.bp-empty__title`, `p.bp-empty__text`, and a button back to the list.

**The consumer provides** the message text, the retry handler and what the missing thing was called.

- Anything that takes more than about 300 ms shows a loading state; the spinner always has words beside it and stops animating under `prefers-reduced-motion`.
- Put an action's error directly under that action (under Read, under Save), never in a banner at the top of the page.
- When a retry fails, either clear the old result or keep it with the "previous result" pill and its time. Never leave a stale result looking current.
- Not-found pages use one wording everywhere: "{Thing} {id} not found", one sentence of explanation, and a button back to the list. Name the real list ("Back to devices"), not "dashboard".
- Don't: use a modal for errors, or color the whole field red; the border, the message and `aria-invalid` carry it.
