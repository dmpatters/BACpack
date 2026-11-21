import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi, beforeEach } from "vitest";
import DeviceBindingsTable from "./DeviceBindingsTable";
import { devicesApi, engineApi } from "../api/endpoints";

vi.mock("../api/endpoints", () => ({
  devicesApi: { list: vi.fn() },
  engineApi: { status: vi.fn() },
}));

function renderTable(props: { defaultBind?: string | null } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <DeviceBindingsTable {...props} />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe("DeviceBindingsTable", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(devicesApi.list).mockResolvedValue([
      { id: 599001, name: "Demo Device" },
      { id: 599002, name: "Bugfix Session Device" },
    ] as never);
  });

  it("lists configured bindings even while nothing is running", async () => {
    vi.mocked(engineApi.status).mockResolvedValue([
      { device_id: 599001, bind_address: "192.168.1.140/24", process_status: "stopped" },
      { device_id: 599002, bind_address: "192.168.1.141/24", process_status: "stopped" },
    ] as never);

    renderTable();

    expect(await screen.findByText("192.168.1.140/24 · UDP 47808")).toBeInTheDocument();
    expect(screen.getByText("192.168.1.141/24 · UDP 47808")).toBeInTheDocument();
    // Device names come from a *second*, independently-resolving useQuery
    // (devicesApi.list) -- the bind-address findByText above only proves the
    // engineApi.status query has settled. Await this one too rather than
    // assuming both mocked queries resolve within the same tick.
    expect(await screen.findByText("Demo Device")).toBeInTheDocument();
  });

  it("marks which bindings are actually live", async () => {
    vi.mocked(engineApi.status).mockResolvedValue([
      { device_id: 599001, bind_address: "192.168.1.140/24", process_status: "running" },
      { device_id: 599002, bind_address: "192.168.1.141/24", process_status: "stopped" },
    ] as never);

    renderTable();

    const runningRow = (await screen.findByText("Demo Device")).closest("tr");
    expect(runningRow).toHaveTextContent(/running/i);
    const stoppedRow = screen.getByText("Bugfix Session Device").closest("tr");
    expect(stoppedRow).toHaveTextContent(/stopped/i);
  });

  it("says when a device has no override and uses the global default", async () => {
    vi.mocked(engineApi.status).mockResolvedValue([
      { device_id: 599001, bind_address: null, process_status: "stopped" },
    ] as never);

    renderTable();

    expect(await screen.findByText(/global default/i)).toBeInTheDocument();
  });

  it("is a design-system table with the address in mono", async () => {
    vi.mocked(engineApi.status).mockResolvedValue([
      { device_id: 599002, bind_address: "192.168.1.141/24", process_status: "stopped" },
    ] as never);
    renderTable();
    expect(await screen.findByRole("table", { name: "Device bindings" })).toHaveClass("bp-table");
    expect(screen.getByText("192.168.1.141/24 · UDP 47808")).toHaveClass("bp-cell-mono");
  });

  it("shows the explicit port of a device on another port", async () => {
    vi.mocked(engineApi.status).mockResolvedValue([
      { device_id: 599001, bind_address: "192.168.1.140/24:47810", process_status: "stopped" },
    ] as never);
    renderTable();
    expect(await screen.findByText("192.168.1.140/24 · UDP 47810")).toBeInTheDocument();
  });

  it("shows the server default, marked, for a device with no address of its own", async () => {
    vi.mocked(engineApi.status).mockResolvedValue([
      { device_id: 599001, bind_address: null, process_status: "stopped" },
    ] as never);
    renderTable({ defaultBind: "192.168.1.140/24" });
    expect(await screen.findByText("192.168.1.140/24 · UDP 47808 (default)")).toBeInTheDocument();
  });
});
