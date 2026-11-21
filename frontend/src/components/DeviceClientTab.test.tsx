import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import DeviceClientTab, { parseValue } from "./DeviceClientTab";
import { clientToolApi, cloneApi } from "../api/endpoints";
import { ApiError } from "../api/http";
import { expectNamedControls } from "../testing/controls";

vi.mock("../api/endpoints", () => ({
  clientToolApi: { whoIs: vi.fn(), objectList: vi.fn(), read: vi.fn(), write: vi.fn(), identify: vi.fn() },
  cloneApi: { start: vi.fn(), get: vi.fn(), cancel: vi.fn(), discard: vi.fn(), plan: vi.fn(), create: vi.fn() },
  versionApi: { get: vi.fn().mockResolvedValue({ version: "0.38.0", service: false }) },
}));

const remote = { instance: 1234, address: "10.0.0.7", vendor_id: 15, max_apdu: 1476, segmentation: "segmented-both" };

function renderTab(isLive = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <DeviceClientTab deviceId={599001} isLive={isLive} />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe("parseValue", () => {
  it("reads JSON where it parses and keeps anything else as text", () => {
    expect(parseValue("72.5")).toBe(72.5);
    expect(parseValue("true")).toBe(true);
    expect(parseValue(" active ")).toBe("active");
  });
});

describe("DeviceClientTab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(clientToolApi.whoIs).mockResolvedValue([remote]);
    vi.mocked(clientToolApi.objectList).mockResolvedValue([
      { object_type: "device", instance: 1234, name: "Remote" },
      { object_type: "analog-value", instance: 3, name: "Setpoint" },
    ]);
    vi.mocked(clientToolApi.read).mockResolvedValue({ value: 72.5 });
    vi.mocked(clientToolApi.write).mockResolvedValue({ ok: true });
    vi.mocked(cloneApi.get).mockRejectedValue(new ApiError("no clone job on this device", 404));
    vi.mocked(clientToolApi.identify).mockResolvedValue({ name: null, vendor_name: null });
  });

  it("names each found device and its vendor, with the vendor id as a hint", async () => {
    vi.mocked(clientToolApi.identify).mockResolvedValue({ name: "AHU-1", vendor_name: "Example Controls" });
    const user = userEvent.setup();
    renderTab();
    await user.click(screen.getByRole("button", { name: "Who-Is" }));

    const table = await screen.findByRole("table", { name: "Discovered devices" });
    expect(table).toHaveClass("bp-table");
    expect(await within(table).findByText("AHU-1")).toBeInTheDocument();
    expect(within(table).getByText("Example Controls")).toBeInTheDocument();
    expect(within(table).getByText("15")).toHaveClass("bp-mono");
    expect(within(table).getByText("Both directions")).toBeInTheDocument();
    expect(within(table).getByText("1476 bytes")).toBeInTheDocument();
    expect(clientToolApi.identify).toHaveBeenCalledWith(599001, { instance: 1234, address: "10.0.0.7", timeout_seconds: 3 });
  });

  it("falls back to the raw vendor id when a device doesn't answer the read", async () => {
    const other = { ...remote, instance: 77, address: "10.0.0.8" };
    vi.mocked(clientToolApi.whoIs).mockResolvedValue([remote, other]);
    vi.mocked(clientToolApi.identify).mockImplementation(async (_id, body) => {
      if (body.instance === 1234) throw new Error("no response within 3 s");
      return { name: "Other", vendor_name: "Example" };
    });
    const user = userEvent.setup();
    renderTab();
    await user.click(screen.getByRole("button", { name: "Who-Is" }));

    const table = await screen.findByRole("table", { name: "Discovered devices" });
    expect(await within(table).findByText("Other")).toBeInTheDocument();
    const silent = within(table).getByText("10.0.0.7").closest("tr") as HTMLElement;
    await waitFor(() => expect(within(silent).queryByText("…")).not.toBeInTheDocument());
    expect(within(silent).getByText("15")).toBeInTheDocument();
    expect(within(silent).getAllByText("—").length).toBeGreaterThan(0);
  });

  it("ignores names that arrive after a newer Who-Is", async () => {
    let release!: (v: { name: string; vendor_name: string }) => void;
    vi.mocked(clientToolApi.identify)
      .mockImplementationOnce(() => new Promise((resolve) => { release = resolve; }))
      .mockResolvedValue({ name: "Fresh", vendor_name: "V" });
    const user = userEvent.setup();
    renderTab();
    await user.click(screen.getByRole("button", { name: "Who-Is" }));
    await screen.findByRole("table", { name: "Discovered devices" });
    await user.click(screen.getByRole("button", { name: "Who-Is" }));
    await screen.findByText("Fresh");
    await act(async () => release({ name: "Stale", vendor_name: "V" }));
    expect(screen.queryByText("Stale")).not.toBeInTheDocument();
    expect(screen.getByText("Fresh")).toBeInTheDocument();
  });

  it("asks at most four devices at once", async () => {
    const many = Array.from({ length: 9 }, (_, i) => ({ ...remote, instance: i, address: `10.0.0.${i}` }));
    vi.mocked(clientToolApi.whoIs).mockResolvedValue(many);
    const pending: (() => void)[] = [];
    vi.mocked(clientToolApi.identify).mockImplementation(
      () => new Promise((resolve) => pending.push(() => resolve({ name: null, vendor_name: null })))
    );
    const user = userEvent.setup();
    renderTab();
    await user.click(screen.getByRole("button", { name: "Who-Is" }));
    await screen.findByRole("table", { name: "Discovered devices" });
    expect(clientToolApi.identify).toHaveBeenCalledTimes(4);
    await act(async () => pending.shift()!());
    expect(clientToolApi.identify).toHaveBeenCalledTimes(5);
  });

  it("stops asking devices to identify once the tab is gone", async () => {
    const many = Array.from({ length: 9 }, (_, i) => ({ ...remote, instance: i, address: `10.0.0.${i}` }));
    vi.mocked(clientToolApi.whoIs).mockResolvedValue(many);
    const pending: (() => void)[] = [];
    vi.mocked(clientToolApi.identify).mockImplementation(
      () => new Promise((resolve) => pending.push(() => resolve({ name: null, vendor_name: null })))
    );
    const user = userEvent.setup();
    const { unmount } = renderTab();
    await user.click(screen.getByRole("button", { name: "Who-Is" }));
    await screen.findByRole("table", { name: "Discovered devices" });
    expect(clientToolApi.identify).toHaveBeenCalledTimes(4);
    unmount();
    await act(async () => pending.splice(0).forEach((resolve) => resolve()));
    expect(clientToolApi.identify).toHaveBeenCalledTimes(4);
  });

  it("moves focus to the browsed device's objects", async () => {
    const user = userEvent.setup();
    renderTab();
    await user.click(screen.getByRole("button", { name: "Who-Is" }));
    await user.click(await screen.findByRole("button", { name: "Browse" }));
    await screen.findByRole("table", { name: "Remote objects" });
    const heading = screen.getByRole("heading", { name: /Device 1234/ });
    expect(heading).toHaveTextContent("Device 1234 · 2 objects");
    expect(heading).toHaveFocus();
  });

  it("explains instead of showing the tool while the device is stopped", () => {
    renderTab(false);

    expect(screen.getByText(/start the device/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Who-Is" })).not.toBeInTheDocument();
  });

  it("runs Who-Is over a range and links a found device to a new connector", async () => {
    const user = userEvent.setup();
    renderTab();

    await user.type(screen.getByLabelText("Low instance"), "1000");
    await user.type(screen.getByLabelText("High instance"), "2000");
    await user.click(screen.getByRole("button", { name: "Who-Is" }));

    expect(clientToolApi.whoIs).toHaveBeenCalledWith(599001, { low: 1000, high: 2000 });
    const table = await screen.findByRole("table", { name: "Discovered devices" });
    expect(within(table).getByText("10.0.0.7")).toBeInTheDocument();
    expect(within(table).getByRole("link", { name: "Create connector" })).toHaveAttribute(
      "href",
      "/connectors?type=bacnet&device=599001&instance=1234"
    );
  });

  it("sends Who-Is without a range when none is given", async () => {
    const user = userEvent.setup();
    renderTab();

    await user.click(screen.getByRole("button", { name: "Who-Is" }));

    expect(clientToolApi.whoIs).toHaveBeenCalledWith(599001, {});
  });

  it("browses, reads, writes and relinquishes a remote point", async () => {
    const user = userEvent.setup();
    renderTab();

    await user.click(screen.getByRole("button", { name: "Who-Is" }));
    await user.click(await screen.findByRole("button", { name: "Browse" }));
    const objects = await screen.findByRole("table", { name: "Remote objects" });
    expect(clientToolApi.objectList).toHaveBeenCalledWith(599001, { instance: 1234, address: "10.0.0.7" });
    await user.click(within(within(objects).getByText("Setpoint").closest("tr")!).getByRole("button", { name: "Select" }));

    await user.click(screen.getByRole("button", { name: "Read" }));
    expect(await screen.findByText("72.5")).toBeInTheDocument();
    expect(clientToolApi.read).toHaveBeenCalledWith(599001, {
      instance: 1234,
      address: "10.0.0.7",
      object_type: "analog-value",
      object_instance: 3,
      property: "present-value",
      array_index: null,
    });

    await user.type(screen.getByLabelText("Value"), "68");
    await user.type(screen.getByLabelText("Priority"), "8");
    await user.click(screen.getByRole("button", { name: "Write" }));
    expect(clientToolApi.write).toHaveBeenLastCalledWith(599001, expect.objectContaining({ value: 68, priority: 8 }));

    await user.click(screen.getByRole("button", { name: "Relinquish" }));
    expect(clientToolApi.write).toHaveBeenLastCalledWith(599001, expect.objectContaining({ value: null, priority: 8 }));
  });

  // "null" in the value box must not make Write send a relinquish.
  it("won't send null with Write", async () => {
    const user = userEvent.setup();
    renderTab();
    await user.click(screen.getByRole("button", { name: "Who-Is" }));
    await user.click(await screen.findByRole("button", { name: "Browse" }));
    const objects = await screen.findByRole("table", { name: "Remote objects" });
    await user.click(within(within(objects).getByText("Setpoint").closest("tr")!).getByRole("button", { name: "Select" }));
    await user.type(screen.getByLabelText("Value"), "null");
    await user.type(screen.getByLabelText("Priority"), "8");
    await user.click(screen.getByRole("button", { name: "Write" }));
    expect(clientToolApi.write).not.toHaveBeenCalled();
    expect(await screen.findByText(/Use Relinquish/)).toBeInTheDocument();
  });

  it("can't relinquish without a priority", async () => {
    const user = userEvent.setup();
    renderTab();

    await user.click(screen.getByRole("button", { name: "Who-Is" }));
    await user.click(await screen.findByRole("button", { name: "Browse" }));
    const objects = await screen.findByRole("table", { name: "Remote objects" });
    await user.click(within(within(objects).getByText("Setpoint").closest("tr")!).getByRole("button", { name: "Select" }));

    expect(screen.getByRole("button", { name: "Relinquish" })).toBeDisabled();
  });

  it("shows a failed read beside Read and drops the earlier result", async () => {
    const user = userEvent.setup();
    renderTab();
    await user.click(screen.getByRole("button", { name: "Who-Is" }));
    await user.click(await screen.findByRole("button", { name: "Browse" }));
    const objects = await screen.findByRole("table", { name: "Remote objects" });
    await user.click(within(within(objects).getByText("Setpoint").closest("tr")!).getByRole("button", { name: "Select" }));
    await user.click(screen.getByRole("button", { name: "Read" }));
    expect(await screen.findByText("72.5")).toBeInTheDocument();

    vi.mocked(clientToolApi.read).mockRejectedValueOnce(new Error("unknown-property"));
    await user.click(screen.getByRole("button", { name: "Read" }));

    const point = screen.getByRole("dialog", { name: /analog-value:3/ });
    expect(await within(point).findByText(/unknown-property/)).toBeInTheDocument();
    expect(screen.queryByText("72.5")).not.toBeInTheDocument();
  });

  it("shows a write's outcome in the point's own box", async () => {
    vi.mocked(clientToolApi.write).mockRejectedValueOnce(new Error("write-access-denied"));
    const user = userEvent.setup();
    renderTab();
    await user.click(screen.getByRole("button", { name: "Who-Is" }));
    await user.click(await screen.findByRole("button", { name: "Browse" }));
    const objects = await screen.findByRole("table", { name: "Remote objects" });
    await user.click(within(within(objects).getByText("Setpoint").closest("tr")!).getByRole("button", { name: "Select" }));
    await user.type(screen.getByLabelText("Value"), "68");
    await user.click(screen.getByRole("button", { name: "Write" }));

    const point = screen.getByRole("dialog", { name: /analog-value:3/ });
    expect(await within(point).findByText(/write-access-denied/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Write" }));
    expect(await within(point).findByText("written")).toBeInTheDocument();
    expect(screen.queryByText(/write-access-denied/)).not.toBeInTheDocument();
  });

  it("shows why a request failed", async () => {
    vi.mocked(clientToolApi.whoIs).mockRejectedValue(new Error("device 599001 didn't answer in time"));
    const user = userEvent.setup();
    renderTab();

    await user.click(screen.getByRole("button", { name: "Who-Is" }));

    expect(await screen.findByText(/didn't answer in time/)).toBeInTheDocument();
  });

  it("opens a selected remote object in a panel named after it; Escape closes it and the table stays", async () => {
    const user = userEvent.setup();
    renderTab();
    await user.click(screen.getByRole("button", { name: "Who-Is" }));
    await user.click(await screen.findByRole("button", { name: "Browse" }));
    const objects = await screen.findByRole("table", { name: "Remote objects" });
    await user.click(within(within(objects).getByText("Setpoint").closest("tr")!).getByRole("button", { name: "Select" }));
    expect(screen.getByRole("dialog", { name: "analog-value:3 (Setpoint)" })).toBeVisible();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: /analog-value:3/ })).not.toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Remote objects" })).toBeInTheDocument();
  });

  it("keeps Who-Is in one toolbar row with labelled range inputs", () => {
    renderTab();
    const bar = screen.getByRole("form", { name: "Who-Is" });
    expect(bar).toHaveClass("bp-toolbar");
    expect(within(bar).getByLabelText("Low instance")).toHaveAttribute("placeholder", "all devices");
    expect(within(bar).getByLabelText("High instance")).toBeInTheDocument();
    expect(within(bar).getByRole("button", { name: "Who-Is" })).toBeInTheDocument();
  });

  it("shows a Who-Is failure under Who-Is, inside its box", async () => {
    vi.mocked(clientToolApi.whoIs).mockRejectedValue(new Error("device 599001 didn't answer in time"));
    const user = userEvent.setup();
    renderTab();
    await user.click(screen.getByRole("button", { name: "Who-Is" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Who-Is failed: device 599001 didn't answer in time");
    const bar = screen.getByRole("form", { name: "Who-Is" });
    expect(bar).not.toContainElement(alert);
    expect(bar.compareDocumentPosition(alert) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  async function selectPoint(user: ReturnType<typeof userEvent.setup>, name: string) {
    const objects = screen.getByRole("table", { name: "Remote objects" });
    await user.click(within(within(objects).getByText(name).closest("tr")!).getByRole("button", { name: "Select" }));
  }

  it("closes the point's panel with its x button", async () => {
    const user = userEvent.setup();
    renderTab();
    await user.click(screen.getByRole("button", { name: "Who-Is" }));
    await user.click(await screen.findByRole("button", { name: "Browse" }));
    await screen.findByRole("table", { name: "Remote objects" });
    await selectPoint(user, "Setpoint");
    await user.click(within(screen.getByRole("dialog", { name: /analog-value:3/ })).getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog", { name: /analog-value:3/ })).not.toBeInTheDocument();
  });

  it("Select on another point replaces the panel and clears the last read and write", async () => {
    const user = userEvent.setup();
    renderTab();
    await user.click(screen.getByRole("button", { name: "Who-Is" }));
    await user.click(await screen.findByRole("button", { name: "Browse" }));
    await screen.findByRole("table", { name: "Remote objects" });
    await selectPoint(user, "Setpoint");
    await user.click(screen.getByRole("button", { name: "Read" }));
    expect(await screen.findByText("72.5")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Value"), "68");
    await user.click(screen.getByRole("button", { name: "Write" }));
    expect(await screen.findByRole("status")).toBeInTheDocument();

    await selectPoint(user, "Remote");
    expect(screen.getByRole("dialog", { name: "device:1234 (Remote)" })).toBeVisible();
    expect(screen.queryByRole("dialog", { name: /analog-value:3/ })).not.toBeInTheDocument();
    expect(screen.queryByText("72.5")).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("Browse closes the point's panel", async () => {
    const user = userEvent.setup();
    renderTab();
    await user.click(screen.getByRole("button", { name: "Who-Is" }));
    await user.click(await screen.findByRole("button", { name: "Browse" }));
    await screen.findByRole("table", { name: "Remote objects" });
    await selectPoint(user, "Setpoint");
    await user.click(screen.getByRole("button", { name: "Browse" }));
    await waitFor(() => expect(clientToolApi.objectList).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: /analog-value:3/ })).not.toBeInTheDocument());
  });

  it("names every control, the picked point's included", async () => {
    const user = userEvent.setup();
    renderTab();
    await user.click(screen.getByRole("button", { name: "Who-Is" }));
    await user.click(await screen.findByRole("button", { name: "Browse" }));
    const objects = await screen.findByRole("table", { name: "Remote objects" });
    await user.click(within(within(objects).getByText("Setpoint").closest("tr")!).getByRole("button", { name: "Select" }));
    expectNamedControls(document.body);
  });

  it("shows a Clone button per Who-Is result", async () => {
    const user = userEvent.setup();
    renderTab();
    await user.click(screen.getByRole("button", { name: "Who-Is" }));
    const found = await screen.findByRole("table", { name: "Discovered devices" });
    expect(within(found).getByRole("button", { name: "Clone" })).toBeInTheDocument();
  });

  it("clicking Clone starts a job and opens the panel", async () => {
    const user = userEvent.setup();
    vi.mocked(cloneApi.start).mockResolvedValue({ job_id: 7, status: "pending" });
    renderTab();
    await user.click(screen.getByRole("button", { name: "Who-Is" }));
    vi.mocked(cloneApi.get).mockResolvedValue({
      job_id: 7, status: "pending", progress: { phase: "object-list", done: 0, total: 0 },
      error: null, created_at: "2026-11-01T12:00:00+00:00", instance: 1234, address: "10.0.0.7",
    });
    await user.click(await screen.findByRole("button", { name: "Clone" }));
    expect(cloneApi.start).toHaveBeenCalledWith(599001, { instance: 1234, address: "10.0.0.7", timeout_seconds: 5 });
    expect(await screen.findByRole("dialog", { name: "Clone device 1234" })).toBeInTheDocument();
  });

  it("reopening the tab with an unfinished job shows its progress", async () => {
    const user = userEvent.setup();
    vi.mocked(cloneApi.get).mockResolvedValue({
      job_id: 7, status: "running", progress: { phase: "objects", done: 3, total: 10 },
      error: null, created_at: "2026-11-01T12:00:00+00:00", instance: 1234, address: null,
    });
    renderTab();
    expect(await screen.findByText("Clone of device 1234: reading 3 of 10 objects.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open clone" }));
    expect(await screen.findByRole("dialog", { name: "Clone device 1234" })).toBeInTheDocument();
  });
});
