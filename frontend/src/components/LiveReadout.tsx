import type { BacnetObjectDTO } from "../api/types";
import StatusPill from "./StatusPill";
import { ageText, useObjectLive, type ObjectLive } from "../utils/useLiveState";
import { controlledFrom } from "../utils/priorities";
import { eventTone, flagTone, formatLiveValue } from "../utils/liveValues";
import { formatUtcTime } from "../utils/time";
import { useNow } from "../utils/useNow";

// What to say when there are no live values to show.
export function liveStatusSentence(live: ObjectLive): string {
  if (live.status === "loading") return "Reading live values…";
  if (live.status === "stopped") return "Device not running: no live values.";
  if (live.status === "missing") return "Not live yet.";
  // Outdated device process, or a failed first read: say what the API said.
  return live.message ?? "";
}

// docs/design-system/components/LiveReadout/README.md
export default function LiveReadout({
  deviceId,
  obj,
  unitLabel,
}: {
  deviceId: number;
  obj: BacnetObjectDTO;
  unitLabel: string;
}) {
  const live = useObjectLive(deviceId, obj.id);
  // Ticks while values are stale, so their age keeps counting.
  const now = useNow(live.status === "stale");

  let body;
  if (!live.data) {
    body = <p className="bp-help">{liveStatusSentence(live)}</p>;
  } else {
    const data = live.data;
    const stale = live.status === "stale";
    const from = controlledFrom(data);
    const item = stale ? "bp-readout__item bp-readout--stale" : "bp-readout__item";
    const stamp = stale ? (
      <span className="bp-readout__stamp">
        {ageText(data.read_at, now)} ·{" "}
        <StatusPill tone="warn" plain>
          stale
        </StatusPill>
      </span>
    ) : (
      <span className="bp-readout__stamp">updated {formatUtcTime(data.read_at)}</span>
    );
    body = (
      <>
        <div className="bp-readout">
          <div className={item}>
            <span className="bp-readout__label">Present value</span>
            <span>
              <span className="bp-readout__value">{formatLiveValue(data.present_value, obj)}</span>
              {unitLabel && <span className="bp-readout__unit">{unitLabel}</span>}
            </span>
            {from && <span className="bp-readout__stamp">{from}</span>}
            {stamp}
          </div>
          {data.status_flags !== null && (
            <div className={item}>
              <span className="bp-readout__label">Status flags</span>
              <span>
                {data.status_flags.length === 0 ? (
                  <StatusPill plain>none</StatusPill>
                ) : (
                  data.status_flags.map((flag) => (
                    <StatusPill key={flag} tone={flagTone(flag)}>
                      {flag}
                    </StatusPill>
                  ))
                )}
              </span>
            </div>
          )}
          {data.event_state !== null && (
            <div className={item}>
              <span className="bp-readout__label">Event state</span>
              <span>
                <StatusPill tone={eventTone(data.event_state)}>{data.event_state}</StatusPill>
              </span>
            </div>
          )}
          {data.reliability && data.reliability !== "no-fault-detected" && (
            <div className={item}>
              <span className="bp-readout__label">Reliability</span>
              <span>
                <StatusPill tone="danger">{data.reliability}</StatusPill>
              </span>
            </div>
          )}
        </div>
      </>
    );
  }
  return <section aria-label="Live values">{body}</section>;
}
