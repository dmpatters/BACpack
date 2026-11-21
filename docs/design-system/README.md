BACpack is a lab and bench tool: it runs virtual BACnet devices and shows their points. The interface is a dense, quiet instrument panel. Data is the content; chrome stays out of the way.

## How to use this system

- Build every screen from the classes in `components/bundle.css` (prefix `bp-`) and the variables in `tokens.css`. Do not write literal colors, sizes or radii in templates.
- Load `tokens.css` first, then `bundle.css`. Both are plain CSS; there is no JavaScript to ship. In the React app, import them once at the root and apply the `bp-` classes with `className`; build React components that render the documented markup rather than restyling per screen.
- Put `data-theme="dark"` on `<html>` by default. `data-theme="light"` swaps every token. Offer a manual toggle or follow `prefers-color-scheme`; the dark theme is the default when nothing is set.
- Offline only. Use the system font stacks `--font-sans` and `--font-mono`. Never link web fonts, icon fonts, CDNs or remote images.

## Visual foundations

- **Surfaces.** Three levels: `bg` for the page, `surface` for the nav, cards and table bodies, `surface-raised` for table headers, hover rows and secondary buttons. Separate with 1px `border` hairlines, not shadows. Only popovers and dialogs use `shadow-popover`.
- **One interactive hue.** `accent` marks links, the active nav item, the active tab, primary buttons and meter fills. Nothing else is blue.
- **Status colors** (`ok`, `warn`, `danger`) mean state only. Every use carries a word (running, failing, WARNING), never color alone. Do not use them as decoration.
- **Text.** `text` for content, `text-muted` for labels, headers, ids, timestamps and help. Do not go dimmer than `text-muted`.
- **Type.** 13px body in the system sans. Anything machine-shaped (object ids, device numbers, addresses, pids, timestamps, URLs, log messages) is `data`, the 12px mono style. Table headers are `table-head`: 11px, uppercase, `text-muted`. One `page-title` per page; `section-title` for sections.
- **Density.** Controls are `control-h` (28px) tall, small row buttons `control-h-sm` (24px), table rows `row-h` (32px). Gaps come from `space-3` to `space-5`; page padding is `space-6`.
- **Radii.** Cards and fieldsets `radius-lg`, controls and callouts `radius-md`, flag chips `radius-sm`, pills and meter bars `radius-pill`.
- **Focus.** Every focusable element gets a 2px solid `focus-ring` outline with a 2px offset. Never remove it.
- **No gradients, glass, emoji icons or decorative illustration.** Do not use side-border accent cards or a centered hero with three feature boxes.

## Layout

- The nav bar spans the full width. Content below it is left-aligned in `bp-page`, up to `content-max` (1280px). Tables, logs and device grids use that full width.
- Forms never stretch: wrap them in `bp-form` (max `form-max`, 680px). Group related fields in a `bp-fieldset` card with an uppercase legend, three fields to a row on desktop.
- Page order: `bp-page-header` (title, optional mono meta, one-line subtitle, page actions on the right), then `bp-tabs` if the page has sections, then content.
- Device home: `bp-device-grid` of `bp-device` cards, with the page action ("New device", "Restart all running devices") in the page header, not below the grid.

## Content fundamentals

- Sentence case everywhere: "Add connector", "Restart serving", "Create device". Button labels are verb-first and short.
- Separate meta items with a middle dot (`&middot;`), never " -- ". Write real plurals: "11 objects", never "object(s)".
- Show timestamps as `YYYY-MM-DD HH:MM:SS` on one line, never wrapped. Put the zone in the column header ("Time (UTC)"), not in every cell.
- Use one zone per screen and label it. Server-side times (logs, traffic, "started", "last restore") are UTC. If a screen shows local time (device clock, trend axis, date pickers), say "local" in its label and format it the same way (ISO date order, 24-hour), never `mm/dd/yyyy`.
- Object ids read `analog-input:1` in `data`, `text-muted`, for local and remote objects alike; never `analog-value,0`. Device numbers are plain digits in `data`.
- Show readable names for enums and codes (units, vendor ids, services) with the raw value as a muted mono hint. Never show raw camelCase or numeric ids alone.
- Write protocol state in plain words: "Communication enabled", "Database revision 1". Don't print `communications: enable`.
- Priority is stated BACnet's way: 1 is highest, 16 lowest. Any label that says "higher wins" must say "lower number wins".
- Long values (URLs, JSON paths) are clipped to one line with `bp-cell-clip` and the full text in a `title`; the detail page shows them in full.
- Units and empty cells: show an em dash for "none", muted.

## Destructive actions

- A page-level delete ("Delete device") is the quiet red button, `bp-btn--danger`, placed in the page header. It opens a confirmation; only the confirmation's button is `bp-btn--danger-solid`.
- Row-level delete is `bp-btn--sm bp-btn--delete`: muted until hover or focus, then red. Never put a solid red button in every table row.
- Imports that replace data ("full sync") show a `bp-callout--warn` above the apply button.

## Behavior rules

- **Loading and errors.** Show a loading state (`AsyncState`) for anything slower than about 300 ms, so a page is never blank. Put an error directly under the action that failed, never in a banner at the top of the page.
- **Unsaved edits.** A form with unsaved changes warns before a tab switch or navigation, or keeps the draft. Never discard edits silently.
- **Addressable views.** Device tabs and object tabs are in the URL (`/devices/599002/process`), and each page sets its own browser tab title ("Bugfix Session Device · Objects · BACpack").
- **Labels.** Every checkbox and input has a visible label bound with `for`/`id`; no control may have the accessible name "on".
- **Forms are collapsed by default.** "Add object" and "Add connector" open on demand and show only the fields for the chosen type; BBMD/foreign fields appear only in that network role, auth fields only when Auth is not None.
- **Stable layout.** `bundle.css` sets `scrollbar-gutter: stable` so content doesn't shift between tabs. Use `bp-nav--sticky` so navigation stays visible.
- **One pattern per job.** Row actions are always an "Edit" link and a quiet Delete; selecting rows swaps the toolbar count for the bulk bar, never adds a row. Lists exist once: the home page is the device list; don't keep a second table version.
- **Phone width.** Layouts hold at 360px: the nav scrolls horizontally, form grids go to one column, device cards stack, tables scroll inside their frame.
- **Destructive actions** ask first with `ConfirmDialog`; imports that replace data show a warn `Callout` and offer a dry-run preview.

## Screens and components

- Devices: `AppNav`, `PageHeader`, `DeviceCard`.
- Device detail: `PageHeader` (name, a run-state `StatusPill`, Start/Stop beside the quiet Delete), `Tabs` (Objects, Process, Traffic, Logs, Import/Export, Settings, BACnet client), `TableToolbar` plus `DataTable` with `FlagChip` and `StatusPill`, `Form` for the add-object fieldset. Flags: W writable, T trended, A alarming, O out of service by default, S simulated.
- Object detail: `PageHeader`, `LiveReadout` (present value, status, event state, priority array), `Tabs` (General, Trending, Alarming, Simulation, plus the object's own tab: Schedule, Recipients or Dates), `Form` with `Combobox` for units and `StateEditor` for multi-state state names. The Trending tab shows `TrendChart`; the Simulation tab is `SimulationEditor`.
- Schedule object: `PageHeader`, `LiveReadout` (present value), `Tabs`, then `ScheduleGrid` for the weekly schedule, `DataTable` plus `Form` for exceptions. Calendar objects use `CalendarEditor` on a tab named "Dates".
- BACnet client tab: `Form` for Who-Is, results in a `DataTable` with vendor names, read errors in `AsyncState` under the Read button.
- Connectors: `DataTable` for the list, `Form` for add and edit, `StatusPill` for connector state, mapping tables as `DataTable`.
- Logs and device Logs and Traffic tabs: `LogTable` (same table, filters above it).
- Trend Storage: `UsageMeter` on top, then a table sorted largest first with inline bars. Do not use a pie chart or a long color legend.
- Network Settings, Config, About: `PageHeader`, `Form`, `Callout`, `DataTable`.
- API Docs: the Swagger page is a different UI; theme it with the same tokens (page background `bg`, panels `surface`, text `text`, method badges as `StatusPill` shapes) and serve its assets locally rather than from a CDN.
