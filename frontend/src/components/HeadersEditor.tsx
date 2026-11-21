import Button from "./Button";
import { Field, FormGrid } from "./Form";

// An http-poll connector's request headers. Values are write-only, like the
// password: a saved header's value is never shown, and a blank one keeps it.
export interface HeaderRow {
  name: string;
  value: string;
  // The name as saved; null for a header added here.
  savedName: string | null;
}

export function savedHeaderRows(names: string[]): HeaderRow[] {
  return names.map((name) => ({ name, value: "", savedName: name }));
}

// The API's headers object: a blank value is null (keep the saved value, or
// not set yet); a row with neither name nor value is dropped.
export function headersBody(rows: HeaderRow[]): Record<string, string | null> {
  const body: Record<string, string | null> = {};
  for (const row of rows) {
    if (!row.name.trim() && !row.value) continue;
    body[row.name.trim()] = row.value || null;
  }
  return body;
}

export default function HeadersEditor({
  rows,
  onChange,
  missing = [],
  unreadable = [],
}: {
  rows: HeaderRow[];
  onChange: (rows: HeaderRow[]) => void;
  // The connector's missing_credentials, and the ones among them whose saved
  // value can't be decrypted.
  missing?: string[];
  unreadable?: string[];
}) {
  const update = (i: number, change: Partial<HeaderRow>) =>
    onChange(rows.map((row, j) => (j === i ? { ...row, ...change } : row)));

  const help = (row: HeaderRow) => {
    if (row.savedName === null || row.savedName !== row.name.trim()) return undefined;
    if (unreadable.includes(`header ${row.savedName}`)) return "Saved value can't be read. Enter it again.";
    return missing.includes(`header ${row.savedName}`) ? "Not set." : "Blank keeps the saved value.";
  };

  return (
    <div className="bp-stack">
      {rows.map((row, i) => (
        <FormGrid key={i}>
          <Field label={`Header ${i + 1} name`}>
            <input type="text" value={row.name} onChange={(e) => update(i, { name: e.target.value })} />
          </Field>
          <Field label={`Header ${i + 1} value`} help={help(row)}>
            <input type="password" value={row.value} onChange={(e) => update(i, { value: e.target.value })} />
          </Field>
          <div className="bp-field">
            <Button
              variant="delete"
              small
              type="button"
              aria-label={`Remove header ${i + 1}`}
              onClick={() => onChange(rows.filter((_, j) => j !== i))}
            >
              Remove
            </Button>
          </div>
        </FormGrid>
      ))}
      <div>
        <Button small type="button" onClick={() => onChange([...rows, { name: "", value: "", savedName: null }])}>
          Add header
        </Button>
      </div>
    </div>
  );
}
