import type { BacnetObjectDTO, LiveObjectDTO } from "../api/types";
import { EmptyValue } from "./DataTable";
import { formatLiveValue } from "../utils/liveValues";
import { mappingValue } from "../utils/mappingValue";
import { unitShort } from "../utils/units";

// A mapping row's last value, beside its status pill, so "ok" is never shown
// without a value (docs/design-system/components/LiveReadout/README.md).
export default function MappingValueCell({
  live,
  direction,
  obj,
}: {
  live: LiveObjectDTO | undefined;
  direction: "read" | "proxy";
  obj: Pick<BacnetObjectDTO, "object_type" | "state_text" | "schedule_value_type" | "units"> | undefined;
}) {
  const value = mappingValue(live, direction);
  if (value === undefined || value === null || !obj) {
    return (
      <td className="bp-cell-mono">
        <EmptyValue />
      </td>
    );
  }
  const unit = unitShort(obj.units);
  return <td className="bp-cell-mono">{`${formatLiveValue(value, obj)}${unit ? ` ${unit}` : ""}`}</td>;
}
