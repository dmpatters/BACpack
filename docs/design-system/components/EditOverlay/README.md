The whole object page in a near-full-screen layer over the Objects list.

**Markup.** `div.bp-overlay` (scrim) > `div.bp-overlay__frame[role="dialog"][aria-modal="true"]` > `div.bp-overlay__bar` with the × "Close" button, then `div.bp-overlay__body` (scrolls). `--overlay-bar-h` is the bar's height; a `bp-panel` inside the overlay starts below it. The page gets `bp-noscroll` while it is up.

**The consumer provides** the title (the object's name) and the page. The nav and page behind it are made `inert`.

- It has its own address (the object's URL): Back, × and Escape close it, and the list underneath is as it was.
- A click on the scrim does not close it; leaving with unsaved input asks first.
- Escape closes the topmost layer only: a confirm dialog, then a panel inside the overlay, then the overlay.
