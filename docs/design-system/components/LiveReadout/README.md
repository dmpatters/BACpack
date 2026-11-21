Live state of a BACnet object: present value, status flags, event state, and the 16-slot priority array.

**Markup.** `div.bp-readout` > `div.bp-readout__item` with `span.bp-readout__label`, `span.bp-readout__value` (mono, tabular) plus `span.bp-readout__unit`, and `span.bp-readout__stamp` for the update time. A commandable object's Present value item also carries a `span.bp-readout__stamp` saying where the value comes from ("from priority 8" / "from Relinquish default"). Add `bp-readout--stale` to the item when the value is old. The 16 priority slots are not in the readout: they are the separate `PriorityColumn` (`bp-prio-col`) beside the object page's tabs.

**The consumer provides** the value, unit label, status flag names, event state, timestamps (UTC, labelled) and the priority slots.

- Put the readout at the top of the object page, above the General/Trending/Alarming/Simulation form, and refresh it on the same interval as the Logs page.
- Status flags and event state are `StatusPill`s with words: "normal" is ok, "in-alarm" danger, "fault" danger, "overridden" warn, "out-of-service" neutral.
- Connector mapping rows show the last value in `bp-cell-mono` next to the status pill, so "ok" is never shown without a value.
- Calendars show whether today is in them as a pill ("today: in calendar" / "not in calendar").
- Stale values turn muted and carry a "stale" pill with the age; never leave an old value looking current.
