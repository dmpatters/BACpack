import type { LogRow } from "../utils/logRows";
import { formatUtcBare } from "../utils/time";
import { EmptyValue, TableFrame } from "./DataTable";
import { LogLevelPill } from "./StatusPill";

// A multi-line message (a traceback) shows its first line; the rest opens
// below it, keeping its line breaks.
function Message({ text }: { text: string }) {
  const newline = text.indexOf("\n");
  if (newline < 0) return <>{text}</>;
  return (
    <details>
      <summary>{text.slice(0, newline)}</summary>
      <pre className="bp-log-trace">{text.slice(newline + 1)}</pre>
    </details>
  );
}

export default function LogTable({
  rows,
  showDeviceColumn,
  emptyText = "No log entries recorded yet.",
}: {
  rows: LogRow[];
  showDeviceColumn: boolean;
  emptyText?: string;
}) {
  if (rows.length === 0) return <p className="bp-muted">{emptyText}</p>;
  return (
    <TableFrame label="Log entries" scroll>
      <thead>
        <tr>
          <th>Time (UTC)</th>
          <th>Level</th>
          {showDeviceColumn && <th>Device</th>}
          <th>Logger</th>
          <th>Message</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((e) => (
          <tr key={e.id}>
            <td className="bp-cell-time">{formatUtcBare(e.timestamp)}</td>
            <td>
              <LogLevelPill level={e.level} />
            </td>
            {showDeviceColumn && <td className="bp-cell-id">{e.device_id ?? <EmptyValue />}</td>}
            <td className="bp-cell-id">{e.logger_name}</td>
            <td className="bp-cell-msg bp-cell-line" title={e.message}>
              <Message text={e.message} />
            </td>
          </tr>
        ))}
      </tbody>
    </TableFrame>
  );
}
