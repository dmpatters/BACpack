import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, RouterProvider, Routes, createMemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import NetworkSettings from "./NetworkSettings";
import { LeaveGuard, UnsavedChangesProvider } from "../components/UnsavedChanges";
import { expectNamedControls } from "../testing/controls";
import { columnOf, inSettingsColumn } from "../testing/columns";
import { BIND_ADDRESS_HELP, BIND_ADDRESS_PLACEHOLDER } from "../utils/bind";
import { devicesApi, engineApi, settingsApi } from "../api/endpoints";
import type { NetworkSettingsDTO } from "../api/types";

vi.mock("../api/endpoints", () => ({
  settingsApi: { get: vi.fn(), update: vi.fn() },
  devicesApi: { list: vi.fn() },
  engineApi: { status: vi.fn(), restartAll: vi.fn() },
  versionApi: { get: vi.fn().mockResolvedValue({ version: "0.32.0", service: false }) },
}));

const settings: NetworkSettingsDTO = {
  bind_address: "192.168.1.140/24",
  trend_retention_days: null,
  trend_soft_limit_mb: null,
};

function renderPage(overrides: Partial<NetworkSettingsDTO> = {}) {
  vi.mocked(settingsApi.get).mockResolvedValue({ ...settings, ...overrides });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <NetworkSettings />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe("NetworkSettings", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(settingsApi.get).mockResolvedValue(settings);
    vi.mocked(devicesApi.list).mockResolvedValue([] as never);
    vi.mocked(engineApi.status).mockResolvedValue([] as never);
  });

  // The page itself offers the restart a saved network setting needs.
  it("has no web UI access card: the login lives on the Security page", async () => {
    renderPage();
    await screen.findByRole("group", { name: "Network" });
    expect(screen.queryByRole("group", { name: "Web UI access" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Username")).not.toBeInTheDocument();
    expect(columnOf(screen.getByRole("heading", { name: "Device bindings" }))).toBeNull();
  });

  it("centres the Network form in the settings column and leaves the bindings table at full width", async () => {
    renderPage();
    expect(inSettingsColumn(await screen.findByRole("group", { name: "Network" }))).toBe(true);
    expect(inSettingsColumn(screen.getByRole("heading", { name: "Device bindings" }))).toBe(false);
    expect(inSettingsColumn(screen.getByRole("heading", { name: "Network Settings" }))).toBe(false);
  });

  const RUNNING = [{ device_id: 1, bind_address: null, process_status: "running" }] as never;

  it("points at the restart-all button after a save while a device is running", async () => {
    vi.mocked(settingsApi.update).mockResolvedValue(settings);
    vi.mocked(engineApi.status).mockResolvedValue(RUNNING);
    renderPage();
    await screen.findByRole("button", { name: "Restart all running devices" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Restart all running devices" })).toBeEnabled());

    await userEvent.setup().click(await screen.findByRole("button", { name: "Save network settings" }));

    expect(
      await screen.findByText('Saved. Use "Restart all running devices" above to apply.')
    ).toBeInTheDocument();
  });

  it("says only Saved, and disables restart-all, when no device is running", async () => {
    vi.mocked(settingsApi.update).mockResolvedValue(settings);
    vi.mocked(devicesApi.list).mockResolvedValue([{ id: 1, name: "Plain" }] as never);
    vi.mocked(engineApi.status).mockResolvedValue([
      { device_id: 1, bind_address: null, process_status: "stopped" },
    ] as never);
    renderPage();
    await screen.findByText("192.168.1.140/24 · UDP 47808 (default)");

    expect(screen.getByRole("button", { name: "Restart all running devices" })).toBeDisabled();
    await userEvent.setup().click(await screen.findByRole("button", { name: "Save network settings" }));

    expect(await screen.findByText("Saved.")).toBeInTheDocument();
    expect(screen.queryByText(/Restart all running devices" above/)).not.toBeInTheDocument();
  });

  it("keeps restart-all enabled, and the restart hint, while the engine status can't be read", async () => {
    vi.mocked(settingsApi.update).mockResolvedValue(settings);
    vi.mocked(engineApi.status).mockRejectedValue(new Error("network down"));
    renderPage();

    const button = await screen.findByRole("button", { name: "Restart all running devices" });
    expect(button).toBeEnabled();
    await userEvent.setup().click(await screen.findByRole("button", { name: "Save network settings" }));
    expect(
      await screen.findByText('Saved. Use "Restart all running devices" above to apply.')
    ).toBeInTheDocument();
  });

  it("states the bind address format in the shared label, hint and placeholder", async () => {
    renderPage();
    const field = await screen.findByLabelText("Default bind address");
    expect(field).toHaveAttribute("placeholder", BIND_ADDRESS_PLACEHOLDER);
    expect(field).toHaveAccessibleDescription(new RegExp(BIND_ADDRESS_HELP.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&")));
  });

  it("restarts every running device from this page", async () => {
    vi.mocked(engineApi.restartAll).mockResolvedValue({ restarted: [1, 2], failed: [] });
    vi.mocked(engineApi.status).mockResolvedValue(RUNNING);
    renderPage();
    await waitFor(() => expect(screen.getByRole("button", { name: "Restart all running devices" })).toBeEnabled());

    await userEvent
      .setup()
      .click(await screen.findByRole("button", { name: "Restart all running devices" }));

    expect(engineApi.restartAll).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("restarted 2 running devices")).toBeInTheDocument();
  });

  it("has no network role: that is set on each device", async () => {
    renderPage();
    await screen.findByLabelText("Default bind address");
    expect(screen.queryByLabelText("Network role")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("BBMD address")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Broadcast Distribution Table peers")).not.toBeInTheDocument();
  });

  it("shows the default bind address with its UDP port in the bindings table", async () => {
    vi.mocked(devicesApi.list).mockResolvedValue([{ id: 1, name: "Plain" }] as never);
    vi.mocked(engineApi.status).mockResolvedValue([
      { device_id: 1, bind_address: null, process_status: "stopped" },
    ] as never);
    renderPage({ bind_address: "192.168.1.140/24:47811" });
    expect(await screen.findByText("192.168.1.140/24 · UDP 47811 (default)")).toBeInTheDocument();
  });

  it("puts a save error under Save network settings", async () => {
    vi.mocked(settingsApi.update).mockRejectedValue(new Error("bind address must be IPv4"));
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Save network settings" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't save: bind address must be IPv4");
  });

  it("saves only the network fields", async () => {
    vi.mocked(settingsApi.update).mockResolvedValue(settings);
    const user = userEvent.setup();
    renderPage();
    const network = await screen.findByRole("group", { name: "Network" });
    await user.clear(within(network).getByLabelText("Default bind address"));
    await user.type(within(network).getByLabelText("Default bind address"), "10.0.0.5/24");
    await user.click(screen.getByRole("button", { name: "Save network settings" }));

    const body = vi.mocked(settingsApi.update).mock.calls[0][0];
    expect(body).toEqual({ bind_address: "10.0.0.5/24" });
  });

  it("names every control", async () => {
    const { container } = renderPage();
    await screen.findByLabelText("Default bind address");
    expectNamedControls(container);
  });

  it("asks before the nav drops an unsaved edit", async () => {
    const user = userEvent.setup();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const router = createMemoryRouter(
      [
        {
          path: "*",
          element: (
            <UnsavedChangesProvider>
              <Routes>
                <Route path="/settings/network" element={<NetworkSettings />} />
                <Route path="*" element={<p>home</p>} />
              </Routes>
              <LeaveGuard />
            </UnsavedChangesProvider>
          ),
        },
      ],
      { initialEntries: ["/settings/network"] }
    );
    render(
      <QueryClientProvider client={client}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    );
    await user.type(await screen.findByLabelText("Default bind address"), "x");
    await act(() => router.navigate("/"));
    expect(screen.getByRole("alertdialog", { name: "Discard unsaved changes?" })).toBeInTheDocument();
  });
});
