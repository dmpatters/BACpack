A form that sits beside a list instead of replacing it: Add object, Edit selected, New device, Add mapping.

**Markup.** `section.bp-panel[role="dialog"][aria-modal="false"]` (labelled by its title) > `header.bp-panel__head` with `h2.bp-panel__title` and a ghost small `×` button named "Close", then `div.bp-panel__body`. Closed, it carries `hidden`; opening plays a short slide-in (off under `prefers-reduced-motion`).

**The consumer provides** the open state, the title and the form. While a panel is open the page's `main` (or an overlay's body) gets `data-panel-open` and reserves the panel's width from 1440px up (narrower, the list would be left too little room); below that the panel sits over the content.

- Fixed to the right edge under the sticky nav, up to the form width (`--form-max`), the full width below 640px.
- Closes with the × button or Escape, never on a click elsewhere: the list beside it stays usable.
- One panel per page; opening another replaces it. Closing hides the panel and keeps what was typed.
- Focus moves to the first field when it opens and back to the opener when it closes. A panel in a hidden tab gives up Escape and the reserved space.
- Inside `bp-overlay` the panel sits at the overlay frame's right edge, under its bar.
