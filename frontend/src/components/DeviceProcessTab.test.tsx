import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import DeviceProcessTab from "./DeviceProcessTab";
import { expectNamedControls } from "../testing/controls";
import { columnOf } from "../testing/columns";
import { BIND_ADDRESS_HELP, BIND_ADDRESS_PLACEHOLDER } from "../utils/bind";
import { capabilitiesApi, devicesApi, engineApi, settingsApi } from "../api/endpoints";
import type { Capabilities, DeviceProcessStatus, DeviceSummary } from "../api/types";

vi.mock("../api/endpoints", () => ({
  devicesApi: { get: vi.fn(), list: vi.fn(), portSuggestion: vi.fn(), setClock: vi.fn(), update: vi.fn() },
  engineApi: { status: vi.fn(), start: vi.fn(), stop: vi.fn(), restart: vi.fn() },
  capabilitiesApi: { get: vi.fn() },
  settingsApi: { get: vi.fn() },
}));

const device: DeviceSummary = {
  id: 1,
  name: "Test Device",
  description: null,
  location: null,
  vendor_identifier: 999,
  vendor_name: null,
  object_count: 0,
  bind_address: null,
};

function renderTab() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <DeviceProcessTab deviceId={1} />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe("DeviceProcessTab", () => {
  beforeEach(() => {
    // A call count left by the test before would pass for this test's own.
    vi.clearAllMocks();
    // Existing tests below don't set this mock; without a default, vi.fn()
    // resolves to undefined and react-query logs "Query data cannot be
    // undefined" noise into the vitest output.
    vi.mocked(capabilitiesApi.get).mockRejectedValue(new Error("not mocked"));
    vi.mocked(devicesApi.list).mockResolvedValue([]);
    vi.mocked(settingsApi.get).mockResolvedValue({ bind_address: null } as never);
  });

  it("shows the communications-enabled state", async () => {
    vi.mocked(devicesApi.get).mockResolvedValue(device);
    const status: DeviceProcessStatus = {
      device_id: 1,
      process_status: "running",
      process_pid: 4242,
      process_started_at: null,
      process_error: null,
      bind_address: null,
      dcc_state: "disable",
      database_revision: 1,
      last_restore_time: null,
      clock_offset_seconds: 0,
      clock_change_pending: false,
      device_time: "2026-09-25T14:03:12-04:00",
    };
    vi.mocked(engineApi.status).mockResolvedValue([status]);

    renderTab();

    expect(await screen.findByTestId("process-info")).toHaveTextContent("Communication disabled");
  });

  // Start, Stop and Restart serving live in the device page header.
  function statusOf(process_status: DeviceProcessStatus["process_status"]): DeviceProcessStatus {
    return {
      device_id: 1,
      process_status,
      process_pid: process_status === "running" ? 4242 : null,
      process_started_at: null,
      process_error: null,
      bind_address: null,
      dcc_state: "enable",
      database_revision: 1,
      last_restore_time: null,
      clock_offset_seconds: 0,
      clock_change_pending: false,
      device_time: "2026-09-25T14:03:12-04:00",
    };
  }

  it("shows the server default with its port for a device without its own address", async () => {
    vi.mocked(devicesApi.get).mockResolvedValue(device);
    vi.mocked(settingsApi.get).mockResolvedValue({ bind_address: "192.168.1.140/24" } as never);
    vi.mocked(engineApi.status).mockResolvedValue([statusOf("running")]);
    renderTab();
    expect(await screen.findByText("192.168.1.140/24 · UDP 47808 (default)")).toBeInTheDocument();
  });

  it("puts the process and clock beside the binding and capabilities", async () => {
    vi.mocked(devicesApi.get).mockResolvedValue(device);
    vi.mocked(engineApi.status).mockResolvedValue([statusOf("running")]);
    vi.mocked(capabilitiesApi.get).mockResolvedValue({
      services: [],
      object_types: [],
      bibb_summary: { met: 0, total: 0 },
    } as unknown as Capabilities);

    renderTab();

    expect(columnOf(await screen.findByTestId("process-info"))).toBe(0);
    expect(columnOf(screen.getByRole("heading", { name: "Device clock" }))).toBe(0);
    expect(columnOf(screen.getByRole("heading", { name: "Network binding" }))).toBe(1);
    expect(columnOf(await screen.findByTestId("capabilities-summary"))).toBe(1);
  });

  it("has no run buttons on a running device: they are in the page header", async () => {
    vi.mocked(devicesApi.get).mockResolvedValue(device);
    vi.mocked(engineApi.status).mockResolvedValue([statusOf("running")]);

    renderTab();

    expect(await screen.findByTestId("process-info")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^(Start|Stop|Restart serving)$/ })).not.toBeInTheDocument();
  });

  it("shows the pid and communications text in the process facts", async () => {
    vi.mocked(devicesApi.get).mockResolvedValue(device);
    const status: DeviceProcessStatus = {
      device_id: 1,
      process_status: "running",
      process_pid: 4242,
      process_started_at: null,
      process_error: null,
      bind_address: null,
      dcc_state: "enable",
      database_revision: 1,
      last_restore_time: null,
      clock_offset_seconds: 0,
      clock_change_pending: false,
      device_time: "2026-09-25T14:03:12-04:00",
    };
    vi.mocked(engineApi.status).mockResolvedValue([status]);

    renderTab();

    expect(await screen.findByTestId("process-info")).toHaveTextContent(/Process id/);
    expect(await screen.findByTestId("process-info")).toHaveTextContent(/Communication enabled/);
  });

  it("has no run buttons while reinitializing either", async () => {
    vi.mocked(devicesApi.get).mockResolvedValue(device);
    vi.mocked(engineApi.status).mockResolvedValue([statusOf("reinitializing")]);

    renderTab();

    expect(await screen.findByTestId("process-info")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^(Start|Stop|Restart serving)$/ })).not.toBeInTheDocument();
  });

  it("says a stopped device isn't running and offers no Start or Stop", async () => {
    vi.mocked(devicesApi.get).mockResolvedValue(device);
    vi.mocked(engineApi.status).mockResolvedValue([statusOf("stopped")]);

    renderTab();

    expect(await screen.findByText(/This device is not running\./)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^(Start|Stop|Restart)/ })).not.toBeInTheDocument();
  });

  const runningStatus: DeviceProcessStatus = {
    device_id: 1,
    process_status: "running",
    process_pid: 4242,
    process_started_at: null,
    process_error: null,
    bind_address: null,
    dcc_state: "enable",
    database_revision: 1,
    last_restore_time: null,
    clock_offset_seconds: 0,
    clock_change_pending: false,
    device_time: "2026-09-25T14:03:12-04:00",
  };

  const caps = {
    bacpack_version: "0.17.0",
    protocol_revision: 22,
    segmentation: "segmented-both",
    max_apdu_length: 1024,
    data_links: ["BACnet/IP"],
    bibb_profile_source: "B-BC, Protocol_Revision 24",
    bibb_summary: { met: 12, total: 21 },
    services: [
      { name: "read-property", kind: "confirmed", executes: true, initiates: false },
      { name: "who-is", kind: "unconfirmed", executes: true, initiates: false },
      { name: "confirmed-event-notification", kind: "confirmed", executes: false, initiates: true },
    ],
    object_types: ["analog-input", "device", "trend-log"],
    bibbs: [],
  } satisfies Capabilities;

  it("summarizes BACnet capabilities with a link to the About page", async () => {
    vi.mocked(devicesApi.get).mockResolvedValue(device);
    vi.mocked(engineApi.status).mockResolvedValue([runningStatus]);
    vi.mocked(capabilitiesApi.get).mockResolvedValue(caps);

    renderTab();

    const summary = await screen.findByTestId("capabilities-summary");
    // Advertised = executed services only (2 here), not initiate-only ones.
    expect(summary).toHaveTextContent(
      "Advertises 2 services · 3 object types · 12/21 B-BC requirements met"
    );
    expect(screen.getByRole("link", { name: /View details/ })).toHaveAttribute(
      "href",
      "/about#capabilities"
    );
  });

  it("says only 'bind address saved' on a running device (the banner says the rest)", async () => {
    const user = userEvent.setup();
    vi.mocked(devicesApi.get).mockResolvedValue(device);
    vi.mocked(devicesApi.update).mockResolvedValue({ ...device, bind_address: "192.168.1.51/24" });
    vi.mocked(engineApi.status).mockResolvedValue([runningStatus]);

    renderTab();

    await user.type(await screen.findByLabelText("Bind address"), "192.168.1.51/24");
    await user.click(screen.getByText("Save bind address"));
    expect(await screen.findByText("Bind address saved.")).toBeInTheDocument();
    expect(screen.queryByText(/Restart serving" above/)).not.toBeInTheDocument();
  });

  it("keeps the saved bind address in the field while the refetch is on its way", async () => {
    const user = userEvent.setup();
    vi.mocked(devicesApi.get).mockReset().mockResolvedValueOnce(device).mockReturnValue(new Promise(() => {}));
    vi.mocked(devicesApi.update).mockResolvedValue({ ...device, bind_address: "192.168.1.51/24" });
    vi.mocked(engineApi.status).mockResolvedValue([runningStatus]);

    renderTab();

    const field = await screen.findByLabelText("Bind address");
    await user.type(field, "192.168.1.51/24");
    await user.click(screen.getByText("Save bind address"));
    expect(await screen.findByText("Bind address saved.")).toBeInTheDocument();
    expect(field).toHaveValue("192.168.1.51/24");
  });

  it("states the bind address format in the shared hint and placeholder", async () => {
    vi.mocked(devicesApi.get).mockResolvedValue(device);
    vi.mocked(engineApi.status).mockResolvedValue([statusOf("stopped")]);
    renderTab();
    const field = await screen.findByLabelText("Bind address");
    expect(field).toHaveAttribute("placeholder", BIND_ADDRESS_PLACEHOLDER);
    expect(field).toHaveAccessibleDescription(`${BIND_ADDRESS_HELP} Blank uses the default.`);
  });

  it("drops the 'applies when the device starts' note once the device is running", async () => {
    const user = userEvent.setup();
    vi.mocked(devicesApi.get).mockResolvedValue(device);
    vi.mocked(devicesApi.update).mockResolvedValue({ ...device, bind_address: "192.168.1.51/24" });
    vi.mocked(engineApi.status).mockResolvedValue([statusOf("stopped")]);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <DeviceProcessTab deviceId={1} />
        </MemoryRouter>
      </QueryClientProvider>
    );
    await user.type(await screen.findByLabelText("Bind address"), "192.168.1.51/24");
    await user.click(screen.getByText("Save bind address"));
    expect(await screen.findByText("Bind address saved. Applies on start.")).toBeInTheDocument();

    vi.mocked(engineApi.status).mockResolvedValue([statusOf("running")]);
    await act(() => client.invalidateQueries({ queryKey: ["engineStatus"] }));

    await waitFor(() => expect(screen.queryByText(/applies when the device starts/)).not.toBeInTheDocument());
  });

  it("omits the capabilities summary when the request fails", async () => {
    vi.mocked(devicesApi.get).mockResolvedValue(device);
    vi.mocked(engineApi.status).mockResolvedValue([runningStatus]);
    vi.mocked(capabilitiesApi.get).mockRejectedValue(new Error("boom"));

    renderTab();

    expect(await screen.findByTestId("process-info")).toBeInTheDocument();
    expect(screen.queryByTestId("capabilities-summary")).not.toBeInTheDocument();
  });

  it("shows the device clock", async () => {
    vi.mocked(devicesApi.get).mockResolvedValue(device);
    const status: DeviceProcessStatus = {
      device_id: 1,
      process_status: "running",
      process_pid: 4242,
      process_started_at: null,
      process_error: null,
      bind_address: null,
      dcc_state: "enable",
      database_revision: 1,
      last_restore_time: null,
      clock_offset_seconds: -3600,
      clock_change_pending: false,
      device_time: "2026-09-25T13:03:12-04:00",
    };
    vi.mocked(engineApi.status).mockResolvedValue([status]);

    renderTab();

    expect(await screen.findByTestId("device-clock-reading")).toHaveTextContent(
      "2026-09-25 13:03:12 device time · -1h 0m 0s behind host"
    );
  });

  it("says the process facts in plain words, each time with its zone", async () => {
    vi.mocked(devicesApi.get).mockResolvedValue(device);
    vi.mocked(engineApi.status).mockResolvedValue([
      {
        ...runningStatus,
        process_started_at: "2026-10-05T23:38:44",
        database_revision: 109,
        last_restore_time: "2026-10-02T16:12:01-04:00",
      },
    ]);

    renderTab();

    const facts = await screen.findByTestId("process-info");
    expect(facts).toHaveClass("bp-facts");
    expect(within(facts).getByText("Communication enabled")).toBeInTheDocument();
    expect(within(facts).getByText("4242")).toBeInTheDocument();
    expect(within(facts).getByText("2026-10-05 23:38:44 UTC")).toBeInTheDocument();
    expect(within(facts).getByText("109")).toBeInTheDocument();
    expect(within(facts).getByText("2026-10-02 16:12:01 device time")).toBeInTheDocument();
    expect(screen.queryByText(/communications:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/ -- /)).not.toBeInTheDocument();
  });

  it("shows the database revision and last restore time", async () => {
    vi.mocked(devicesApi.get).mockResolvedValue(device);
    const status: DeviceProcessStatus = {
      device_id: 1,
      process_status: "running",
      process_pid: 4242,
      process_started_at: null,
      process_error: null,
      bind_address: null,
      dcc_state: "enable",
      database_revision: 12,
      last_restore_time: "2026-09-26T10:30:00-04:00",
      clock_offset_seconds: 0,
      clock_change_pending: false,
      device_time: "2026-09-25T14:03:12-04:00",
    };
    vi.mocked(engineApi.status).mockResolvedValue([status]);

    renderTab();

    const info = await screen.findByTestId("process-info");
    expect(info).toHaveClass("bp-facts");
    expect(within(info).getByText("12")).toBeInTheDocument();
    expect(within(info).getByText("2026-09-26 10:30:00 device time")).toBeInTheDocument();
    expect(info).not.toHaveTextContent("-04:00");
  });

  it("shows never when the device has not been restored", async () => {
    vi.mocked(devicesApi.get).mockResolvedValue(device);
    const status: DeviceProcessStatus = {
      device_id: 1,
      process_status: "running",
      process_pid: 4242,
      process_started_at: null,
      process_error: null,
      bind_address: null,
      dcc_state: "enable",
      database_revision: 1,
      last_restore_time: null,
      clock_offset_seconds: 0,
      clock_change_pending: false,
      device_time: "2026-09-25T14:03:12-04:00",
    };
    vi.mocked(engineApi.status).mockResolvedValue([status]);

    renderTab();

    expect(within(await screen.findByTestId("process-info")).getByText("Never")).toBeInTheDocument();
  });

  it("shows a bind address save error under Save bind address", async () => {
    const user = userEvent.setup();
    vi.mocked(devicesApi.get).mockResolvedValue(device);
    vi.mocked(devicesApi.update).mockRejectedValue(new Error("bind_address: not an IPv4 address"));
    vi.mocked(engineApi.status).mockResolvedValue([runningStatus]);

    renderTab();

    await user.type(await screen.findByLabelText("Bind address"), "nope");
    await user.click(screen.getByRole("button", { name: "Save bind address" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Couldn't save the bind address: bind_address: not an IPv4 address");
    expect(screen.getByRole("button", { name: "Save bind address" }).closest("form")).toContainElement(alert);
  });

  it("names every control", async () => {
    vi.mocked(devicesApi.get).mockResolvedValue(device);
    vi.mocked(engineApi.status).mockResolvedValue([runningStatus]);
    const { container } = renderTab();
    await screen.findByLabelText("Bind address");
    expectNamedControls(container);
  });

  it("shows the bind address with its UDP port", async () => {
    vi.mocked(devicesApi.get).mockResolvedValue(device);
    vi.mocked(engineApi.status).mockResolvedValue([{ ...runningStatus, bind_address: "192.168.1.51/24:47810" }]);
    renderTab();
    expect(await within(await screen.findByTestId("process-info")).findByText("192.168.1.51/24 · UDP 47810")).toBeInTheDocument();
  });

  it("offers a free port under a crash from a port in use", async () => {
    vi.mocked(devicesApi.get).mockResolvedValue(device);
    vi.mocked(devicesApi.portSuggestion).mockResolvedValue({ bind_address: "192.168.1.200/24:47810", port: 47810 });
    vi.mocked(engineApi.status).mockResolvedValue([
      {
        ...statusOf("crashed"),
        process_error: "UDP port 192.168.1.200:47808 is already in use by another program or device",
        port_in_use: true,
      },
    ]);
    renderTab();
    expect(await screen.findByRole("button", { name: "Use port 47810" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Other ways round this" })).toBeInTheDocument();
  });

  it("offers no port help for another kind of crash", async () => {
    vi.mocked(devicesApi.get).mockResolvedValue(device);
    vi.mocked(engineApi.status).mockResolvedValue([
      { ...statusOf("crashed"), process_error: "Address 192.168.1.200 is not assigned to any network interface" },
    ]);
    renderTab();
    expect(await screen.findByText(/Crashed: Address 192\.168\.1\.200/)).toBeInTheDocument();
    expect(screen.queryByTestId("port-in-use-help")).not.toBeInTheDocument();
    expect(devicesApi.portSuggestion).not.toHaveBeenCalled();
  });

  it("puts the network role beside the bind address", async () => {
    vi.mocked(devicesApi.get).mockResolvedValue(device);
    vi.mocked(engineApi.status).mockResolvedValue([runningStatus]);
    renderTab();
    expect(columnOf(await screen.findByLabelText("Network role"))).toBe(1);
  });
});
