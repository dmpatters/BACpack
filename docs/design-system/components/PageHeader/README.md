The title block at the top of every page: h1, optional mono meta, one-line subtitle, and page-level actions on the right.

**Markup.** `header.bp-page-header` > `div` holding `h1.bp-page-header__title` (with `span.bp-page-header__meta` for the device number), `p.bp-page-header__sub`; page actions as a sibling of that `div`. Wrap page content in `div.bp-page` (max `content-max`, padded `space-6`).

**The consumer provides** the title, the optional meta (device number, object id), the subtitle and the actions.

- One `h1` per page, in `page-title`.
- The subtitle is a short description or the device description, at most about 80 characters wide; longer explanations belong in a `Callout`.
- Put the quiet red "Delete device" button here, on the right. Don't put primary actions in the header unless the page has nothing else to do.
