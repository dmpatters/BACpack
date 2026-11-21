The top navigation bar: brand, eight section links, version tag.

**Markup.** `nav.bp-nav` > `a.bp-nav__brand`, `div.bp-nav__links` > `a.bp-nav__link`, `span.bp-nav__version`. Set `aria-current="page"` on the link for the current section; that is the only active style.

**The consumer provides** the links, the current-page flag and the version string (`data` style, from the server).

- Keep the links in this order: Devices, Connectors, Trend Storage, Logs, Network Settings, Config, API Docs, About.
- The bar is full width; the page content below it is not (see `PageHeader`). Add `bp-nav--sticky` so it stays visible while scrolling.
- Link text matches the page title, and each link goes to the route the app already uses ("Trend Storage" is `/trends`, "Network Settings" is `/settings/network`).  Do not rename routes; existing bookmarks keep working.
- API Docs opens in the same tab.  It is BACpack's own docs page, not FastAPI's default: the same nav bar, themed with the tokens, Swagger assets served locally.
- At phone width the version hides and the links scroll sideways.
- Do: label the nav `aria-label="Main"`. Don't: add icons, dropdowns or a second row of links.
