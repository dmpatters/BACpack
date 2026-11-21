Single-letter chips that show which BACnet object features are on.

**Markup.** `span.bp-flags` containing `span.bp-flag` with a `title` for each: W writable, T trended, A alarming, O out of service by default, S simulated. Each chip carries its full name in a `span.bp-visually-hidden` and hides the letter from assistive technology (`aria-hidden`), so the meaning doesn't depend on the hover `title`.

**The consumer provides** the set flags only; an unset flag is omitted, not greyed out.

- Always include a `title` (and keep the letter uppercase) so the meaning is available on hover and to assistive tech.
- Fixed order: W, T, A, O, S.
- Don't: use flag chips for status (use `StatusPill`) or color individual flags.
