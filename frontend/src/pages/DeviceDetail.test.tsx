import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import DeviceDetail from "./DeviceDetail";
import { devicesApi, engineApi } from "../api/endpoints";
import { ApiError } from "../api/http";

vi.mock("../api/endpoints", () => ({
  devicesApi: { get: vi.fn(), remove: vi.fn() },
  engineApi: { status: vi.fn(), start: vi.fn(), stop: vi.fn() },
}));
vi.mock("../components/DeviceObjectsTab", () => ({ default: () => <p>objects panel</p> }));
vi.mock("../components/DeviceProcessTab", () => ({ default: () => <p>process panel</p> }));
vi.mock("../components/DeviceTrafficTab", () => ({ default: () => <p>traffic panel</p> }));
vi.mock("../components/DeviceLogsTab", () => ({ default: () => <p>logs panel</p> }));
vi.mock("../components/DeviceImportExportTab", () => ({ default: () => <p>import-export panel</p> }));
vi.mock("../components/DeviceSettingsTab", () => ({ default: () => <p>settings panel</p> }));
vi.mock("../components/DeviceClientTab", () => ({ default: () => <p>client panel</p> }));

const DEVICE = { id: 599002, name: "Bugfix Session Device", description: "", object_count: 0, bind_address: null };

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/devices/:deviceId/:tab?" element={<DeviceDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
  return client;
}

describe("DeviceDetail", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(engineApi.status).mockResolvedValue([] as never);
  });

  it("says the device wasn't found on a 404", async () => {
    vi.mocked(devicesApi.get).mockRejectedValue(new ApiError("Device 999999 not found", 404));
    renderAt("/devices/999999");
    expect(await screen.findByRole("heading", { name: "Device 999999 not found" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to devices" })).toHaveAttribute("href", "/");
  });

  it("says it couldn't load the device when the server fails another way", async () => {
    vi.mocked(devicesApi.get).mockRejectedValue(new Error("Failed to fetch"));
    renderAt("/devices/599002");
    expect(await screen.findByRole("heading", { name: "Couldn't load device 599002" })).toBeInTheDocument();
    expect(screen.getByText("Failed to fetch")).toBeInTheDocument();
  });

  it("says a non-numeric device id isn't found, without asking the server", async () => {
    renderAt("/devices/abc");
    expect(await screen.findByRole("heading", { name: "Device abc not found" })).toBeInTheDocument();
    expect(devicesApi.get).not.toHaveBeenCalled();
    expect(document.title).toBe("Device abc not found · BACpack");
  });

  it("titles a missing device with its not-found heading", async () => {
    vi.mocked(devicesApi.get).mockRejectedValue(new ApiError("Device 999999 not found", 404));
    renderAt("/devices/999999");
    await screen.findByRole("heading", { name: "Device 999999 not found" });
    expect(document.title).toBe("Device 999999 not found · BACpack");
  });

  it("titles a failed load with its heading", async () => {
    vi.mocked(devicesApi.get).mockRejectedValue(new Error("Failed to fetch"));
    renderAt("/devices/599002");
    await screen.findByRole("heading", { name: "Couldn't load device 599002" });
    expect(document.title).toBe("Couldn't load device 599002 · BACpack");
  });

  it("opens the tab named in the URL and titles the page with it", async () => {
    vi.mocked(devicesApi.get).mockResolvedValue(DEVICE as never);
    renderAt("/devices/599002/process");
    expect(await screen.findByText("process panel")).toBeVisible();
    expect(screen.getByRole("link", { name: "Process" })).toHaveAttribute("aria-current", "page");
    expect(document.title).toBe("Bugfix Session Device · Process · BACpack");
  });

  it("shows the run state with Start, and deletes only after confirming", async () => {
    const user = userEvent.setup();
    vi.mocked(devicesApi.get).mockResolvedValue(DEVICE as never);
    vi.mocked(engineApi.status).mockResolvedValue([{ device_id: 599002, process_status: "stopped" }] as never);
    vi.mocked(devicesApi.remove).mockResolvedValue(undefined as never);
    renderAt("/devices/599002");
    expect(await screen.findByText("stopped")).toHaveClass("bp-pill");
    expect(screen.getByRole("button", { name: "Start" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Delete device" }));
    expect(screen.getByRole("alertdialog", { name: "Delete device 599002?" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(devicesApi.remove).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Delete device" }));
    await user.click(screen.getAllByRole("button", { name: "Delete device" }).at(-1)!);
    expect(devicesApi.remove).toHaveBeenCalledWith(599002);
  });

  it("shows a failed start as a notice below the header, not beside Start, and clears it once running", async () => {
    const user = userEvent.setup();
    vi.mocked(devicesApi.get).mockResolvedValue(DEVICE as never);
    vi.mocked(engineApi.status).mockResolvedValue([{ device_id: 599002, process_status: "stopped" }] as never);
    vi.mocked(engineApi.start).mockRejectedValue(new Error("UDP port 47808 is in use"));
    const client = renderAt("/devices/599002");
    const start = await screen.findByRole("button", { name: "Start" });
    await user.click(start);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("UDP port 47808 is in use");
    expect(start.parentElement).not.toContainElement(alert);
    expect(alert.closest("header")).toBeNull();

    vi.mocked(engineApi.status).mockResolvedValue([{ device_id: 599002, process_status: "running" }] as never);
    await act(() => client.invalidateQueries({ queryKey: ["engineStatus"] }));
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  });

  it("clears the start error as soon as the next attempt begins", async () => {
    const user = userEvent.setup();
    vi.mocked(devicesApi.get).mockResolvedValue(DEVICE as never);
    vi.mocked(engineApi.status).mockResolvedValue([{ device_id: 599002, process_status: "stopped" }] as never);
    vi.mocked(engineApi.start).mockRejectedValueOnce(new Error("UDP port 47808 is in use"));
    renderAt("/devices/599002");
    await user.click(await screen.findByRole("button", { name: "Start" }));
    await screen.findByRole("alert");

    vi.mocked(engineApi.start).mockReturnValue(new Promise(() => {}) as never);
    await user.click(screen.getByRole("button", { name: "Start" }));
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  });

  it("drops the start error of one device when the page moves to another", async () => {
    const user = userEvent.setup();
    vi.mocked(devicesApi.get).mockResolvedValue(DEVICE as never);
    vi.mocked(engineApi.status).mockResolvedValue([
      { device_id: 599002, process_status: "stopped" },
      { device_id: 599003, process_status: "stopped" },
    ] as never);
    vi.mocked(engineApi.start).mockRejectedValue(new Error("UDP port 47808 is in use"));
    function Other() {
      const navigate = useNavigate();
      return <button onClick={() => navigate("/devices/599003")}>other device</button>;
    }
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/devices/599002"]}>
          <Routes>
            <Route
              path="/devices/:deviceId/:tab?"
              element={
                <>
                  <Other />
                  <DeviceDetail />
                </>
              }
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );
    await user.click(await screen.findByRole("button", { name: "Start" }));
    await screen.findByRole("alert");

    await user.click(screen.getByRole("button", { name: "other device" }));
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  });

  it("counts the objects as they are when the dialog opens", async () => {
    const user = userEvent.setup();
    vi.mocked(devicesApi.get)
      .mockResolvedValueOnce(DEVICE as never)
      .mockResolvedValue({ ...DEVICE, object_count: 40 } as never);
    renderAt("/devices/599002");
    await user.click(await screen.findByRole("button", { name: "Delete device" }));
    expect(await screen.findByText(/its 40 objects and their trend history/)).toBeInTheDocument();
  });

  it("confirms once and stays open while the delete runs", async () => {
    const user = userEvent.setup();
    vi.mocked(devicesApi.get).mockResolvedValue(DEVICE as never);
    vi.mocked(devicesApi.remove).mockReturnValue(new Promise(() => {}) as never);
    renderAt("/devices/599002");
    await user.click(await screen.findByRole("button", { name: "Delete device" }));
    const confirm = screen.getAllByRole("button", { name: "Delete device" }).at(-1)!;
    await user.click(confirm);
    expect(confirm).toBeDisabled();
    await user.click(confirm);
    await user.click(screen.getByTestId("confirm-scrim"));
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    expect(devicesApi.remove).toHaveBeenCalledTimes(1);
  });

  it("says so for a tab the device page doesn't have", async () => {
    vi.mocked(devicesApi.get).mockResolvedValue(DEVICE as never);
    renderAt("/devices/599002/bogus");
    expect(await screen.findByRole("heading", { name: 'No "bogus" tab' })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to Bugfix Session Device" })).toHaveAttribute(
      "href",
      "/devices/599002"
    );
  });
});
