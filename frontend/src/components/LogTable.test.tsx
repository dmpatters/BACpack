import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import LogTable from "./LogTable";
import type { LogEntry } from "../api/types";

const entries: LogEntry[] = [
  {
    id: 1,
    timestamp: "2026-09-09T12:00:00Z",
    level: "INFO",
    logger_name: "bacpack.engine",
    message: "Engine started",
    device_id: 3,
  },
  {
    id: 2,
    timestamp: "2026-09-09T12:01:00Z",
    level: "ERROR",
    logger_name: "bacpack.web",
    message: "Request failed",
    device_id: null,
  },
];

const traceback: LogEntry = {
  id: 3,
  timestamp: "2026-09-09T12:02:00Z",
  level: "ERROR",
  logger_name: "bacpack.engine",
  message: "reattach failed for device 3\nTraceback (most recent call last):\n  File \"engine.py\", line 640\nOSError: boom",
  device_id: 3,
};

describe("LogTable", () => {
  it("renders its empty text when there are no rows", () => {
    render(<LogTable rows={[]} showDeviceColumn={false} />);
    expect(screen.getByText("No log entries recorded yet.")).toBeInTheDocument();
    render(<LogTable rows={[]} showDeviceColumn={false} emptyText="No entries match." />);
    expect(screen.getByText("No entries match.")).toBeInTheDocument();
  });

  it("heads the time column UTC and keeps the zone out of the cells", () => {
    render(<LogTable rows={entries} showDeviceColumn={false} />);
    expect(screen.getByRole("columnheader", { name: "Time (UTC)" })).toBeInTheDocument();
    expect(screen.getByText("2026-09-09 12:00:00")).toHaveClass("bp-cell-time");
  });

  it("shows levels as plain pills and ids in mono", () => {
    render(<LogTable rows={entries} showDeviceColumn={true} />);
    expect(screen.getByText("ERROR")).toHaveClass("bp-pill", "bp-pill--plain", "bp-pill--danger");
    expect(screen.getByText("bacpack.engine")).toHaveClass("bp-cell-id");
    expect(screen.getByRole("columnheader", { name: "Device" })).toBeInTheDocument();
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("omits the Device column when asked", () => {
    render(<LogTable rows={entries} showDeviceColumn={false} />);
    expect(screen.queryByRole("columnheader", { name: "Device" })).not.toBeInTheDocument();
  });

  it("keeps a traceback to one line until opened, then shows it preformatted", async () => {
    const user = userEvent.setup();
    render(<LogTable rows={[traceback]} showDeviceColumn={false} />);
    const summary = screen.getByText("reattach failed for device 3");
    const trace = screen.getByText(/Traceback \(most recent call last\)/);
    expect(trace).not.toBeVisible();
    await user.click(summary);
    expect(trace).toBeVisible();
    expect(trace.tagName).toBe("PRE");
    expect(trace).toHaveClass("bp-log-trace");
    expect(trace.textContent).toContain('\n  File "engine.py"');
  });

  it("keeps an open traceback open when a newer entry arrives on top", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<LogTable rows={[traceback]} showDeviceColumn={false} />);
    await user.click(screen.getByText("reattach failed for device 3"));
    rerender(<LogTable rows={[{ ...entries[0], id: 4 }, traceback]} showDeviceColumn={false} />);
    expect(screen.getByText(/Traceback \(most recent call last\)/)).toBeVisible();
  });

  it("clips a message to one line and keeps the whole text in its title", () => {
    const long = "reattached to 4 already-running devices: 599003 (pid 11680), 599002 (pid 11679), 599001 (pid 11678), 100000 (pid 11677)";
    render(<LogTable rows={[{ ...entries[0], message: long }, traceback]} showDeviceColumn={false} />);
    const cell = screen.getByText(long);
    expect(cell).toHaveClass("bp-cell-msg", "bp-cell-line");
    expect(cell).toHaveAttribute("title", long);
    const traceCell = screen.getByText("reattach failed for device 3").closest("td");
    expect(traceCell).toHaveClass("bp-cell-line");
    expect(traceCell).toHaveAttribute("title", traceback.message);
  });

  it("shows a collapsed row's count", () => {
    render(<LogTable rows={[{ ...entries[0], message: "reattached to 4 already-running devices: …", count: 4 }]} showDeviceColumn />);
    expect(screen.getByText(/reattached to 4 already-running devices/)).toBeInTheDocument();
  });
});
