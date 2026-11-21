import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import LogView from "./LogView";
import { devicesApi, logsApi } from "../api/endpoints";
import type { LogEntry } from "../api/types";
import { collapseReattached } from "../utils/logRows";

vi.mock("../api/endpoints", () => ({
  logsApi: { list: vi.fn() },
  devicesApi: { list: vi.fn() },
}));
vi.mock("../utils/logRows", async (importOriginal) => {
  const real = await importOriginal<typeof import("../utils/logRows")>();
  return { ...real, collapseReattached: vi.fn(real.collapseReattached) };
});

let nextId = 1;
const e = (message: string, logger_name = "bacpack.engine"): LogEntry => ({
  id: nextId++,
  timestamp: "2026-10-13T10:00:00Z",
  level: "INFO",
  logger_name,
  message,
  device_id: 599001,
});

function renderView(deviceId?: number) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <LogView deviceId={deviceId} />
    </QueryClientProvider>
  );
}

describe("LogView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(devicesApi.list).mockResolvedValue([{ id: 599001, name: "AHU sim" }] as never);
    vi.mocked(logsApi.list).mockResolvedValue([e("connector Weather polled"), e("device 599001 started"), e("poll failed", "bacpack.connectors")]);
  });
  afterEach(() => vi.useRealTimers());

  it("searches the loaded entries by message or logger", async () => {
    const user = userEvent.setup();
    renderView();
    await screen.findByText("device 599001 started");
    await user.type(screen.getByLabelText("Search"), "connectors");
    expect(screen.getByText("poll failed")).toBeInTheDocument();
    expect(screen.queryByText("device 599001 started")).not.toBeInTheDocument();
    await user.clear(screen.getByLabelText("Search"));
    await user.type(screen.getByLabelText("Search"), "nothing like this");
    expect(screen.getByText('No entries match "nothing like this".')).toBeInTheDocument();
  });

  it("doesn't reprocess the loaded entries on each search keystroke", async () => {
    const user = userEvent.setup();
    renderView();
    await screen.findByText("device 599001 started");
    const calls = vi.mocked(collapseReattached).mock.calls.length;
    await user.type(screen.getByLabelText("Search"), "poll");
    expect(screen.queryByText("device 599001 started")).not.toBeInTheDocument();
    expect(vi.mocked(collapseReattached).mock.calls.length).toBe(calls);
  });

  it("filters by device on the server-wide page only", async () => {
    const user = userEvent.setup();
    renderView();
    // The select is there at once; the device list arrives after it.
    await screen.findByRole("option", { name: "AHU sim (599001)" });
    await user.selectOptions(screen.getByLabelText("Device"), "599001");
    expect(logsApi.list).toHaveBeenLastCalledWith({ device_id: 599001, level: "INFO", limit: 200 });
    expect(screen.queryByRole("columnheader", { name: "Device" })).not.toBeInTheDocument();
  });

  it("has no device filter on a device's own tab", async () => {
    renderView(599001);
    await screen.findByText("device 599001 started");
    expect(screen.queryByLabelText("Device")).not.toBeInTheDocument();
    expect(logsApi.list).toHaveBeenCalledWith({ device_id: 599001, level: "INFO", limit: 200 });
  });

  it("stops refreshing while paused", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderView();
    await screen.findByText("device 599001 started");
    await act(() => vi.advanceTimersByTimeAsync(5500));
    const before = vi.mocked(logsApi.list).mock.calls.length;
    expect(before).toBeGreaterThan(1);
    await user.click(screen.getByRole("button", { name: "Pause updates" }));
    expect(screen.getByRole("button", { name: "Resume updates" })).toHaveAttribute("aria-pressed", "true");
    await act(() => vi.advanceTimersByTimeAsync(12000));
    expect(vi.mocked(logsApi.list).mock.calls.length).toBe(before);
  });

  it("loads older entries 200 at a time, keeping the rows on screen meanwhile", async () => {
    const user = userEvent.setup();
    const page = Array.from({ length: 200 }, (_, i) => e(`entry ${i}`));
    vi.mocked(logsApi.list).mockResolvedValueOnce(page).mockReturnValue(new Promise(() => {}));
    renderView();
    await screen.findByText("entry 0");
    expect(screen.getByText("Showing the newest 200 entries")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Load older" }));
    expect(logsApi.list).toHaveBeenLastCalledWith({ level: "INFO", limit: 400 });
    expect(screen.getByText("entry 0")).toBeInTheDocument();
  });

  it("drops the old filter's rows and its older pages when the filter changes", async () => {
    const user = userEvent.setup();
    const page = Array.from({ length: 200 }, (_, i) => e(`entry ${i}`));
    const twoPages = [...page, ...Array.from({ length: 200 }, (_, i) => e(`older ${i}`))];
    vi.mocked(logsApi.list)
      .mockResolvedValueOnce(page)
      .mockResolvedValueOnce(twoPages)
      .mockReturnValue(new Promise(() => {}));
    renderView();
    await screen.findByRole("option", { name: "AHU sim (599001)" });
    await screen.findByText("entry 0");
    await user.click(screen.getByRole("button", { name: "Load older" }));
    await screen.findByText("older 0");
    await user.selectOptions(screen.getByLabelText("Device"), "599001");
    expect(logsApi.list).toHaveBeenLastCalledWith({ device_id: 599001, level: "INFO", limit: 200 });
    expect(screen.queryByText("entry 0")).not.toBeInTheDocument();
    expect(await screen.findByText("Loading log entries…")).toBeInTheDocument();
  });

  it("offers no older entries when the last load came back short", async () => {
    renderView();
    await screen.findByText("device 599001 started");
    expect(screen.getByRole("button", { name: "Load older" })).toBeDisabled();
  });
});
