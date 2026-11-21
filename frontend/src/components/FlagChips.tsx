import type { BacnetObjectDTO } from "../api/types";
import { EmptyValue } from "./DataTable";

type Flagged = Pick<BacnetObjectDTO, "bacnet_writable" | "trended" | "alarming_enabled" | "out_of_service_default" | "simulation">;

const FLAGS: Array<[(o: Flagged) => boolean, string, string]> = [
  [(o) => o.bacnet_writable, "W", "BACnet writable"],
  [(o) => o.trended, "T", "Trended"],
  [(o) => o.alarming_enabled, "A", "Alarming"],
  [(o) => o.out_of_service_default, "O", "Out of service by default"],
  [(o) => o.simulation != null, "S", "Simulated"],
];

export default function FlagChips({ obj }: { obj: Flagged }) {
  const set = FLAGS.filter(([on]) => on(obj));
  if (set.length === 0) return <EmptyValue />;
  return (
    <span className="bp-flags">
      {set.map(([, letter, title]) => (
        <span key={letter} className="bp-flag" title={title}>
          <span aria-hidden="true">{letter}</span>
          <span className="bp-visually-hidden">{title}</span>
        </span>
      ))}
    </span>
  );
}
