The Simulation tab: choose how an object's value moves, set only the fields that kind needs, and see an example of the result beside the form.

**Markup.** `div.bp-sim` with two children: `form.bp-sim__form` (the enable checkbox with its help line, then `bp-fieldset` groups) and `div.bp-sim__preview` holding a `bp-chart` (see `TrendChart`) drawn from the current field values. Add `bp-sim--off` when simulation is switched off.

**The consumer provides** the object's family, the chosen kind and its field values, the object's unit, state names for multi-state objects, and the code that redraws the preview.

**Simulation fieldset first.** It holds one control, the kind.  Everything below it depends on that choice; nothing for another kind is rendered, not even disabled.  Use a `bp-select` when a family has more than three kinds, a `bp-segmented` radio group when it has two.  Switching kind keeps values the new kind shares (update interval, decimals, limits) and resets the rest to that kind's defaults.

| Family | Kinds |
|---|---|
| Analog (AI, AO, AV) | Sine, Triangle, Sawtooth, Square, Random walk, Noise, Follow another object |
| Binary (BI, BO, BV) | Square, Random |
| Multi-state (MSI, MSO, MSV) | Cycle through states, Random |

There is no Constant kind and no start rule.  There is no seed, so random kinds cannot be repeated.

**Fields by kind** (defaults in brackets; the simulator is the source of truth).

| Kind | Signal | Timing |
|---|---|---|
| Sine | Center (50), Amplitude ± (10) | Period s (600), Phase offset s |
| Triangle, Sawtooth | Low (0), High (100) | Period s (600), Phase offset s |
| Square (analog) | Low (0), High (100) | Period s (600), Duty 0-1 (0.5), Phase offset s |
| Random walk | Start (50), Max step per update (1), Low (0), High (100) | |
| Noise | Center (50), Amplitude ± (1) | |
| Follow another object | Source object, Scale, Offset, Noise ± (optional) | |
| Binary square | | Period s (120), Duty 0-1 (0.5), Phase offset s |
| Binary random | | Shortest dwell s (30), Longest dwell s (120) |
| Multi-state cycle | States (optional) | Hold each state s (60), Phase offset s |
| Multi-state random | States (optional) | Shortest dwell s (30), Longest dwell s (120) |

Every analog kind also has Update every (s) (5, 1 or more) in Timing, and an Output group: Decimals (2, 0 to 6), Never below and Never above (both optional, blank means no limit, never-below not above never-above).  Binary and multi-state objects update every second and have no interval, decimals or limits, so they have no Output group.  A group with no fields for the chosen kind is not shown.

- **Phase offset** is in seconds, 0 or more, optional, and shown only on the cycling kinds (sine, triangle, sawtooth, square, cycle).  Help text: "Shifts the cycle against the clock."
- **Source object** is a `Combobox` of analog objects on the same device, shown as "analog-value:3 · name".
- **States** (multi-state kinds) is a checkbox list of the object's states by number and name, all checked by default.  Blank or all-checked means every state.  At least two must be checked.  It replaces the comma-separated text box.
- **Priority.** The simulator writes the present value at priority 10, only when the value changes.  Say so in one help line under the enable checkbox.

**Preview.**
- The cycle is tied to the wall clock, not to when simulation started, so the x axis is real time ("time (UTC)", `HH:MM` ticks), never "minutes from start".  Use the simulator's own clock reference so the shape and the phase offset match what the device will do.
- It redraws as the fields change, before saving, and is marked "not live" so it is never mistaken for logged data.
- Window: about three periods, between 5 and 20 minutes; for random kinds 20 minutes.  One series, `accent` line, same chart rules as `TrendChart`, the unit on y.
- Random kinds (Random walk, Noise, Random) show one example run and say so in the footer ("One example run").  They are not repeatable.
- Binary and multi-state kinds draw a step chart with the state names on the y axis.
- Follow another object has nothing to draw.  Replace the chart with two `LiveReadout` items: the source's value now and what this object would show, with the footer "Writes nothing while the source is missing".
- Footer states the kind and its settings in words and the time zone.

**Behavior.**
- Fields sit in a grid, never a column of full-width rows.  Units in labels ("Period (s)"); one line of help only where it adds meaning.
- Check as you type: low must be below high; never-below not above never-above; period above 0; amplitude 0 or more; start between low and high; step above 0; dwell 1 or more and shortest not above longest; update interval 1 or more.  Show the message under the field and keep the last valid preview, dimmed.
- "Simulate this object" is a real labelled checkbox.  When off, the fields are disabled (`fieldset[disabled]`) and the preview dims.  "Reset to defaults" is ghost and never saves by itself.
- At width under about 900px the preview moves above the fields.
- Don't: show fields for kinds that are not selected, invent a pattern editor with a separate time per state (the simulator has period and duty, or one hold time for all states), show the preview only after saving, make it look like logged data, or print a formula.
