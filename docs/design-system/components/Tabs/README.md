Underlined tabs for the sections of a device or an object.

**Markup.** `nav.bp-tabs` > `a.bp-tab` (or `button.bp-tab` with `role="tab"`). Mark the current one with `aria-current="page"` for links or `aria-selected="true"` for buttons.

**The consumer provides** the sections and which one is current. Server-rendered tabs are links to their own URLs, so reload and back keep working.

- Device tabs, in order: Objects, Process, Traffic, Logs, Import/Export, Settings, BACnet client. Object tabs: General, Trending, Alarming, Simulation.
- Tabs sit directly under the page header, above all content; the hairline runs the full width.
- Don't: nest tabs, use them for actions, or add counts in the label (put counts in the content heading, "Objects (55)").
