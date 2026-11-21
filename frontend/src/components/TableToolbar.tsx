import type { ReactNode } from "react";
import Button from "./Button";

// "Over about 15 rows" (docs/design-system/components/TableToolbar).
export const TOOLBAR_MIN_ROWS = 16;

// For a table whose toolbar shows only from TOOLBAR_MIN_ROWS: a search left
// typed when the table shrinks below that would filter with no box to clear it.
export function toolbarSearch(search: string, rowCount: number): string {
  return rowCount >= TOOLBAR_MIN_ROWS ? search : "";
}

export function countText(shown: number, total: number, noun: string, plural = `${noun}s`): string {
  const word = total === 1 ? noun : plural;
  return shown === total ? `${total} ${word}` : `${shown} of ${total} ${word}`;
}

// The bulk bar takes the count's place in the same fixed-height row, so
// selecting a row never moves the table under the cursor.
export default function TableToolbar({
  search,
  onSearch,
  searchLabel,
  placeholder,
  filter,
  count,
  selectedCount = 0,
  bulkActions,
  onClearSelection,
  actions,
}: {
  search: string;
  onSearch: (value: string) => void;
  searchLabel: string;
  placeholder?: string;
  filter?: {
    label: string;
    value: string;
    onChange: (value: string) => void;
    options: Array<{ value: string; label: string }>;
  };
  count: ReactNode;
  selectedCount?: number;
  bulkActions?: ReactNode;
  onClearSelection?: () => void;
  // The page's own button, at the end of the row.
  actions?: ReactNode;
}) {
  return (
    <div className="bp-toolbar">
      <input
        className="bp-input bp-toolbar__search"
        type="search"
        aria-label={searchLabel}
        placeholder={placeholder}
        value={search}
        onChange={(e) => onSearch(e.target.value)}
      />
      {filter && (
        <select
          className="bp-select bp-toolbar__select"
          aria-label={filter.label}
          value={filter.value}
          onChange={(e) => filter.onChange(e.target.value)}
        >
          {filter.options.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      )}
      {selectedCount > 0 ? (
        <div className="bp-bulkbar" role="group" aria-label="Bulk actions">
          <span className="bp-bulkbar__count">{selectedCount} selected</span>
          {bulkActions}
          {onClearSelection && (
            <Button small variant="ghost" type="button" onClick={onClearSelection}>
              Clear selection
            </Button>
          )}
        </div>
      ) : (
        <span className="bp-toolbar__count" aria-live="polite">
          {count}
        </span>
      )}
      {actions}
    </div>
  );
}
