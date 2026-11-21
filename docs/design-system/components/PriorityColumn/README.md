The 16-slot priority array of a commandable object, in a column beside the object page's tabs.

**Markup.** `aside.bp-prio-col` > a `button.bp-prio-col__toggle` ("Show priority names" / "Hide priority names", `aria-expanded`, only below 1100px), `ol.bp-prio-col__list` of 16 `li.bp-prio-col__row` (`span.bp-prio-col__n`, `span.bp-prio-col__name`, `span.bp-prio-col__value`), then `div.bp-prio-col__default` for the Relinquish default under a rule. The controlling slot has `aria-current="true"`; a set slot below it (a higher number) has `data-outranked`; set slots have `data-set`. Add `bp-prio-col--stale` when the values are old.

**The consumer provides** the slots, the controlling priority, the Relinquish default and the unit. Named priorities: 1 Manual life safety, 2 Automatic life safety, 5 Critical equipment control, 6 Minimum on/off, 8 Manual operator.

- About 230px wide and sticky on a wide screen, the same on every tab. Below 1100px it becomes a block above the tabs: 16 small slots in two rows, the full named list behind the toggle.
- Objects without a priority array have no column.
- The Present value card in `LiveReadout` says which slot is in control ("from priority 8" / "from Relinquish default").
