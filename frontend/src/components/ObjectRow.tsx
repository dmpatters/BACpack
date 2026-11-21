import type { BacnetObjectDTO } from "../api/types";
import { ClipCell, EmptyValue, RowActions } from "./DataTable";
import FlagChips from "./FlagChips";
import { OVERLAY_FROM_LIST } from "../utils/overlayState";

interface Props {
  obj: BacnetObjectDTO;
  deviceId: number;
  selected: boolean;
  onToggleSelect: (id: number) => void;
  onDelete: (id: number) => void;
}

// Read-only by design: no page width fits a row of editable controls. Editing
// lives in the object's overlay, and multi-object edits go through
// BulkEditPanel.
export default function ObjectRow({ obj, deviceId, selected, onToggleSelect, onDelete }: Props) {
  return (
    <tr data-selected={selected || undefined}>
      <td className="bp-cell-check">
        <input type="checkbox" checked={selected} onChange={() => onToggleSelect(obj.id)} aria-label={`Select ${obj.name}`} />
      </td>
      <td className="bp-cell-id">
        {obj.object_type}:{obj.instance_number}
      </td>
      <td>{obj.name}</td>
      <ClipCell value={obj.description} />
      <td>{obj.units ?? <EmptyValue />}</td>
      <td>
        <FlagChips obj={obj} />
      </td>
      <RowActions
        editTo={`/devices/${deviceId}/objects/${obj.id}`}
        editState={OVERLAY_FROM_LIST}
        onDelete={() => onDelete(obj.id)}
      />
    </tr>
  );
}
