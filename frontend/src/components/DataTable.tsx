import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import Button, { buttonClass } from "./Button";
import type { SortState } from "../utils/tableSort";

// The design system's DataTable markup (docs/design-system/components/DataTable).
// A scrolling frame is focusable so it can be scrolled from the keyboard.
export function TableFrame({ label, scroll = false, children }: { label: string; scroll?: boolean; children: ReactNode }) {
  return (
    <div
      className={scroll ? "bp-table-wrap bp-table-wrap--scroll" : "bp-table-wrap"}
      tabIndex={scroll ? 0 : undefined}
      role={scroll ? "region" : undefined}
      aria-label={scroll ? label : undefined}
    >
      <table className="bp-table" aria-label={label}>
        {children}
      </table>
    </div>
  );
}

export function SortHeader<K extends string>({
  label,
  sortKey,
  sort,
  onSort,
  className,
}: {
  label: string;
  sortKey: K;
  sort: SortState<K>;
  onSort: (key: K) => void;
  className?: string;
}) {
  return (
    <th className={className} aria-sort={sort?.key === sortKey ? sort.dir : undefined}>
      <button type="button" className="bp-sort" onClick={() => onSort(sortKey)}>
        {label}
      </button>
    </th>
  );
}

export function EmptyValue() {
  return <span className="bp-muted">—</span>;
}

export function ClipCell({ value, className }: { value: string | null | undefined; className?: string }) {
  if (!value) {
    return (
      <td className={className}>
        <EmptyValue />
      </td>
    );
  }
  return (
    <td className={className ? `bp-cell-clip ${className}` : "bp-cell-clip"} title={value}>
      {value}
    </td>
  );
}

export function RowActions({
  editTo,
  editState,
  onEdit,
  onDelete,
  deleteDisabled = false,
}: {
  editTo?: string;
  editState?: unknown;
  onEdit?: () => void;
  onDelete: () => void;
  deleteDisabled?: boolean;
}) {
  return (
    <td className="bp-cell-actions">
      {editTo !== undefined ? (
        <Link className={buttonClass("ghost", true)} to={editTo} state={editState}>
          Edit
        </Link>
      ) : (
        onEdit && (
          <Button variant="ghost" small type="button" onClick={onEdit}>
            Edit
          </Button>
        )
      )}
      <Button variant="delete" small type="button" disabled={deleteDisabled} onClick={onDelete}>
        Delete
      </Button>
    </td>
  );
}
