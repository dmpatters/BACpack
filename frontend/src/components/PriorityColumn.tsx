import { useState } from "react";
import type { BacnetObjectDTO } from "../api/types";
import { formatLiveValue } from "../utils/liveValues";
import { PRIORITY_NAMES } from "../utils/priorities";
import { unitShort } from "../utils/units";
import { useObjectLive } from "../utils/useLiveState";
import { liveStatusSentence } from "./LiveReadout";

// The 16 slots of a commandable object, beside the object page's tabs. It
// shares the readout's live query. Below 1100px it shrinks to a compact grid
// of numbers until its toggle shows the full named list.
export default function PriorityColumn({ deviceId, obj }: { deviceId: number; obj: BacnetObjectDTO }) {
  const live = useObjectLive(deviceId, obj.id);
  const [expanded, setExpanded] = useState(false);

  if (!live.data) {
    if (live.status === "loading") return null;
    return (
      <aside className="bp-prio-col" aria-label="Priority array">
        <p className="bp-help">{liveStatusSentence(live)}</p>
      </aside>
    );
  }
  const { priority_array: slots, controlling_priority: controlling } = live.data;
  const unit = unitShort(obj.units);
  if (!slots) return null;

  return (
    <aside
      className={live.status === "stale" ? "bp-prio-col bp-prio-col--stale" : "bp-prio-col"}
      aria-label="Priority array"
      data-expanded={expanded ? "" : undefined}
    >
      <button
        type="button"
        className="bp-prio-col__toggle bp-btn bp-btn--ghost bp-btn--sm"
        aria-expanded={expanded}
        onClick={() => setExpanded((open) => !open)}
      >
        {expanded ? "Hide priority names" : "Show priority names"}
      </button>
      <ol className="bp-prio-col__list" aria-label="Priority slots">
        {slots.map((value, i) => {
          const priority = i + 1;
          const outranked = value !== null && controlling !== null && priority > controlling;
          return (
            <li
              key={priority}
              className="bp-prio-col__row"
              aria-current={controlling === priority ? "true" : undefined}
              data-outranked={outranked ? "" : undefined}
              data-set={value !== null ? "" : undefined}
            >
              <span className="bp-prio-col__n">{priority}</span>{" "}
              <span className="bp-prio-col__name">{PRIORITY_NAMES[priority] ?? ""}</span>{" "}
              <span className="bp-prio-col__value">{value === null ? "—" : formatLiveValue(value, obj)}</span>
            </li>
          );
        })}
      </ol>
      <div className="bp-prio-col__default">
        <span className="bp-prio-col__name">Relinquish default</span>{" "}
        <span className="bp-prio-col__value">
          {formatLiveValue(live.data.relinquish_default, obj)}
          {live.data.relinquish_default !== null && unit && ` ${unit}`}
        </span>
      </div>
    </aside>
  );
}
