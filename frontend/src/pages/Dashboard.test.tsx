import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, RouterProvider, Routes, createMemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import Dashboard from "./Dashboard";
import { authApi, devicesApi, engineApi, sampleApi, versionApi } from "../api/endpoints";

vi.mock("../api/endpoints", () => ({
  devicesApi: { list: vi.fn(), create: vi.fn() },
  edeApi: { read: vi.fn(), plan: vi.fn(), create: vi.fn() },
  sampleApi: { plan: vi.fn(), load: vi.fn() },
  engineApi: { status: vi.fn(), start: vi.fn(), stop: vi.fn(), restartAll: vi.fn() },
  versionApi: { get: vi.fn().mockResolvedValue({ version: "0.32.0", service: false }) },
  settingsApi: { get: vi.fn().mockResolvedValue({ bind_address: null }) },
  authApi: { state: vi.fn().mockResolvedValue({ mode: "login", signed_in: true, username: "admin" }) },
}));

describe("Dashboard", () => {
  it("is the Devices page, with its actions in the header", async () => {
    vi.mocked(devicesApi.list).mockResolvedValue([] as never);
    vi.mocked(engineApi.status).mockResolvedValue([] as never);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <Dashboard />
        </MemoryRouter>
      </QueryClientProvider>
    );
    expect(await screen.findByRole("heading", { level: 1, name: "Devices" })).toBeInTheDocument();
    expect(screen.getByText("Simulated BACnet devices")).toBeInTheDocument();
    expect(screen.queryByText(/simulator \+ gateway/)).not.toBeInTheDocument();
    const header = screen.getByRole("banner");
    expect(within(header).getByRole("link", { name: "New device" })).toHaveAttribute("href", "/devices/new");
    expect(document.title).toBe("Devices · BACpack");
  });

  // With one shared pending id, starting a second device would re-enable
  // the first one's button while its start was still running.
  it("keeps each device busy until its own request finishes", async () => {
    vi.mocked(devicesApi.list).mockResolvedValue([
      { id: 1, name: "Dev A", description: null, vendor_identifier: 999, vendor_name: null, object_count: 0, bind_address: null },
      { id: 2, name: "Dev B", description: null, vendor_identifier: 999, vendor_name: null, object_count: 0, bind_address: null },
    ] as never);
    vi.mocked(engineApi.status).mockResolvedValue([] as never);
    vi.mocked(engineApi.start).mockReturnValue(new Promise(() => {}) as never);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <Dashboard />
        </MemoryRouter>
      </QueryClientProvider>
    );
    const user = userEvent.setup();
    await screen.findByText("Dev B");
    const [startA, startB] = screen.getAllByRole("button", { name: "Start" });
    await user.click(startA);
    await user.click(startB);
    expect(startA).toBeDisabled();
    expect(startB).toBeDisabled();
  });

  it("clears an earlier error banner once a start succeeds", async () => {
    vi.mocked(devicesApi.list).mockResolvedValue([
      { id: 1, name: "Dev A", description: null, vendor_identifier: 999, vendor_name: null, object_count: 0, bind_address: null },
    ] as never);
    vi.mocked(engineApi.status).mockResolvedValue([] as never);
    vi.mocked(engineApi.start)
      .mockReset()
      .mockRejectedValueOnce(new Error("UDP port 47808 is already in use"))
      .mockResolvedValueOnce({} as never);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <Dashboard />
        </MemoryRouter>
      </QueryClientProvider>
    );
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Start" }));
    expect(await screen.findByText("UDP port 47808 is already in use")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Start" }));
    await vi.waitFor(() => expect(engineApi.start).toHaveBeenCalledTimes(2));
    await vi.waitFor(() =>
      expect(screen.queryByText("UDP port 47808 is already in use")).not.toBeInTheDocument()
    );
  });

  it("restarts every running device from the home page", async () => {
    vi.mocked(devicesApi.list).mockResolvedValue([
      { id: 1, name: "Dev A", description: null, vendor_identifier: 999, vendor_name: null, object_count: 0, bind_address: null },
    ] as never);
    vi.mocked(engineApi.status).mockResolvedValue([] as never);
    vi.mocked(engineApi.restartAll).mockResolvedValue({ restarted: [1], failed: [] });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <Dashboard />
        </MemoryRouter>
      </QueryClientProvider>
    );

    await userEvent
      .setup()
      .click(await screen.findByRole("button", { name: "Restart all running devices" }));

    expect(engineApi.restartAll).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("restarted 1 running device")).toBeInTheDocument();
  });

  it("offers Restart UI only under a service even with no devices", async () => {
    vi.mocked(devicesApi.list).mockResolvedValue([] as never);
    vi.mocked(engineApi.status).mockResolvedValue([] as never);
    vi.mocked(versionApi.get).mockResolvedValueOnce({ version: "0.32.1", service: true });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <Dashboard />
        </MemoryRouter>
      </QueryClientProvider>
    );

    expect(await screen.findByRole("button", { name: "Restart UI only" })).toBeInTheDocument();
    expect(screen.getByText("No devices yet.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Restart all running devices" })).toBeNull();
  });
});

describe("Dashboard New device panel", () => {
  const device = { id: 1, name: "Dev A", description: null, vendor_identifier: 999, vendor_name: null, object_count: 0, bind_address: null };

  function renderRouted(path: string) {
    vi.mocked(devicesApi.list).mockResolvedValue([device] as never);
    vi.mocked(engineApi.status).mockResolvedValue([] as never);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const router = createMemoryRouter(
      [
        {
          path: "*",
          element: (
            <main tabIndex={-1}>
              <Routes>
                <Route path="/devices" element={<Dashboard />} />
                <Route path="/devices/new" element={<Dashboard />} />
                <Route path="*" element={<p>elsewhere</p>} />
              </Routes>
            </main>
          ),
        },
      ],
      { initialEntries: [path] }
    );
    render(
      <QueryClientProvider client={client}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    );
    return router;
  }

  it("opens at /devices/new beside the device cards and closes with the x", async () => {
    const user = userEvent.setup();
    const router = renderRouted("/devices");
    await screen.findByText("Dev A");
    await user.click(screen.getByRole("link", { name: "New device" }));
    expect(router.state.location.pathname).toBe("/devices/new");
    const panel = screen.getByRole("dialog", { name: "New device" });
    expect(panel).toBeVisible();
    expect(screen.getByText("Dev A")).toBeInTheDocument();

    await user.click(within(panel).getByRole("button", { name: "Close" }));
    expect(router.state.location.pathname).toBe("/devices");
    expect(screen.queryByRole("dialog", { name: "New device" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "New device" })).toHaveFocus();
  });

  it("closes with Escape and with Back", async () => {
    const user = userEvent.setup();
    const router = renderRouted("/devices");
    await screen.findByText("Dev A");
    await user.click(screen.getByRole("link", { name: "New device" }));
    await user.keyboard("{Escape}");
    expect(router.state.location.pathname).toBe("/devices");
    expect(screen.queryByRole("dialog", { name: "New device" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "New device" }));
    expect(screen.getByRole("dialog", { name: "New device" })).toBeVisible();
    await act(() => router.navigate(-1));
    expect(router.state.location.pathname).toBe("/devices");
    expect(screen.queryByRole("dialog", { name: "New device" })).not.toBeInTheDocument();
  });

  it("opens from a link to /devices/new and closes to /devices by replace", async () => {
    const user = userEvent.setup();
    const router = renderRouted("/devices/new");
    const panel = await screen.findByRole("dialog", { name: "New device" });
    expect(await screen.findByText("Dev A")).toBeInTheDocument();
    await user.click(within(panel).getByRole("button", { name: "Close" }));
    expect(router.state.location.pathname).toBe("/devices");
    expect(router.state.historyAction).toBe("REPLACE");
  });

  it("keeps what was typed when closed and opened again", async () => {
    const user = userEvent.setup();
    renderRouted("/devices");
    await screen.findByText("Dev A");
    await user.click(screen.getByRole("link", { name: "New device" }));
    await user.type(screen.getByLabelText(/^name/i), "Half typed");
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("link", { name: "New device" }));
    expect(screen.getByLabelText(/^name/i)).toHaveValue("Half typed");
  });
});

describe("Dashboard New device panel: history, focus and creating", () => {
  const device = { id: 1, name: "Dev A", description: null, vendor_identifier: 999, vendor_name: null, object_count: 0, bind_address: null };

  function renderRouted(path: string) {
    vi.mocked(devicesApi.list).mockResolvedValue([device] as never);
    vi.mocked(engineApi.status).mockResolvedValue([] as never);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const router = createMemoryRouter(
      [
        {
          path: "*",
          element: (
            <main tabIndex={-1}>
              <Routes>
                <Route path="/devices" element={<Dashboard />} />
                <Route path="/devices/new" element={<Dashboard />} />
                <Route path="/devices/import-ede" element={<Dashboard />} />
                <Route path="/devices/:id" element={<p>device page</p>} />
              </Routes>
            </main>
          ),
        },
      ],
      { initialEntries: [path] }
    );
    render(
      <QueryClientProvider client={client}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    );
    return router;
  }

  it("needs one x after a second click on New device", async () => {
    const user = userEvent.setup();
    const router = renderRouted("/devices");
    await screen.findByText("Dev A");
    await user.click(screen.getByRole("link", { name: "New device" }));
    await user.click(screen.getByRole("link", { name: "New device" }));
    await user.click(within(screen.getByRole("dialog", { name: "New device" })).getByRole("button", { name: "Close" }));
    expect(router.state.location.pathname).toBe("/devices");
  });

  it("needs one x after a second click on Import EDE", async () => {
    const user = userEvent.setup();
    const router = renderRouted("/devices");
    await screen.findByText("Dev A");
    await user.click(screen.getByRole("link", { name: "Import EDE" }));
    await user.click(screen.getByRole("link", { name: "Import EDE" }));
    await user.click(within(screen.getByRole("dialog", { name: "Import EDE" })).getByRole("button", { name: "Close" }));
    expect(router.state.location.pathname).toBe("/devices");
  });

  it("puts focus on main when a panel opened by a link closes", async () => {
    const user = userEvent.setup();
    renderRouted("/devices/new");
    const panel = await screen.findByRole("dialog", { name: "New device" });
    await user.click(within(panel).getByRole("button", { name: "Close" }));
    expect(screen.getByRole("main")).toHaveFocus();
  });

  it("goes to the new device after creating it", async () => {
    vi.mocked(devicesApi.create).mockResolvedValue({ id: 599100 } as never);
    const user = userEvent.setup();
    const router = renderRouted("/devices");
    await screen.findByText("Dev A");
    await user.click(screen.getByRole("link", { name: "New device" }));
    await user.type(screen.getByLabelText(/instance number/i), "599100");
    await user.type(screen.getByLabelText(/^name/i), "AHU-3");
    await user.click(screen.getByRole("button", { name: /create device/i }));
    await vi.waitFor(() => expect(router.state.location.pathname).toBe("/devices/599100"));
  });
});

describe("Dashboard Import EDE panel", () => {
  const device = { id: 1, name: "Dev A", description: null, vendor_identifier: 999, vendor_name: null, object_count: 0, bind_address: null };

  function renderRouted(path: string) {
    vi.mocked(devicesApi.list).mockResolvedValue([device] as never);
    vi.mocked(engineApi.status).mockResolvedValue([] as never);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const router = createMemoryRouter(
      [
        {
          path: "*",
          element: (
            <main tabIndex={-1}>
              <Routes>
                <Route path="/devices" element={<Dashboard />} />
                <Route path="/devices/new" element={<Dashboard />} />
                <Route path="/devices/import-ede" element={<Dashboard />} />
              </Routes>
            </main>
          ),
        },
      ],
      { initialEntries: [path] }
    );
    render(
      <QueryClientProvider client={client}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    );
    return router;
  }

  it("the Import EDE button opens its panel at /devices/import-ede", async () => {
    const user = userEvent.setup();
    const router = renderRouted("/devices");
    await screen.findByText("Dev A");
    const header = screen.getByRole("banner");
    await user.click(within(header).getByRole("link", { name: "Import EDE" }));
    expect(router.state.location.pathname).toBe("/devices/import-ede");
    expect(screen.getByRole("dialog", { name: "Import EDE" })).toBeVisible();
    expect(screen.getByLabelText("EDE file")).toBeInTheDocument();
  });

  it("opening one panel closes the other, and one x lands on the plain page", async () => {
    const user = userEvent.setup();
    const router = renderRouted("/devices");
    await screen.findByText("Dev A");
    await user.click(screen.getByRole("link", { name: "New device" }));
    expect(screen.getByRole("dialog", { name: "New device" })).toBeVisible();
    await user.click(screen.getByRole("link", { name: "Import EDE" }));
    expect(screen.queryByRole("dialog", { name: "New device" })).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Import EDE" })).toBeVisible();
    await user.click(screen.getByRole("link", { name: "New device" }));
    expect(screen.queryByRole("dialog", { name: "Import EDE" })).not.toBeInTheDocument();
    await user.click(within(screen.getByRole("dialog", { name: "New device" })).getByRole("button", { name: "Close" }));
    expect(router.state.location.pathname).toBe("/devices");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("opened from a link, its x goes to /devices by replace", async () => {
    const user = userEvent.setup();
    const router = renderRouted("/devices/import-ede");
    const panel = await screen.findByRole("dialog", { name: "Import EDE" });
    await user.click(within(panel).getByRole("button", { name: "Close" }));
    expect(router.state.location.pathname).toBe("/devices");
    expect(router.state.historyAction).toBe("REPLACE");
  });
});

describe("Dashboard sample building panel", () => {
  const device = { id: 1, name: "Dev A", description: null, vendor_identifier: 999, vendor_name: null, object_count: 0, bind_address: null };

  function renderRouted(path: string, devices: unknown[]) {
    vi.mocked(devicesApi.list).mockResolvedValue(devices as never);
    vi.mocked(engineApi.status).mockResolvedValue([] as never);
    vi.mocked(sampleApi.plan).mockResolvedValue({
      devices: [{ id: 4190001, name: "Sample AHU-1", role: "bbmd", object_count: 13, bind_address: "127.0.0.1/8:47808", port: 47808 }],
      refusal: null,
      notes: [],
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const router = createMemoryRouter(
      [
        {
          path: "*",
          element: (
            <main tabIndex={-1}>
              <Routes>
                <Route path="/devices" element={<Dashboard />} />
                <Route path="/devices/sample" element={<Dashboard />} />
              </Routes>
            </main>
          ),
        },
      ],
      { initialEntries: [path] }
    );
    render(
      <QueryClientProvider client={client}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    );
    return router;
  }

  it("offers Load sample building when there are no devices, opening its panel at /devices/sample", async () => {
    const user = userEvent.setup();
    const router = renderRouted("/devices", []);
    const link = await screen.findByRole("link", { name: "Load sample building" });
    expect(link).toHaveAttribute("href", "/devices/sample");
    await user.click(link);
    expect(router.state.location.pathname).toBe("/devices/sample");
    expect(await screen.findByRole("dialog", { name: "Sample building" })).toBeVisible();
  });

  it("does not offer it once devices exist", async () => {
    renderRouted("/devices", [device]);
    await screen.findByText("Dev A");
    expect(screen.queryByRole("link", { name: "Load sample building" })).not.toBeInTheDocument();
  });

  it("opened from a link, its x goes to /devices by replace", async () => {
    const user = userEvent.setup();
    const router = renderRouted("/devices/sample", [device]);
    const panel = await screen.findByRole("dialog", { name: "Sample building" });
    await user.click(within(panel).getByRole("button", { name: "Close" }));
    expect(router.state.location.pathname).toBe("/devices");
    expect(router.state.historyAction).toBe("REPLACE");
  });
});

describe("Dashboard sample building load", () => {
  it("lands on /devices with the panel closed after a successful load", async () => {
    const user = userEvent.setup();
    vi.mocked(devicesApi.list).mockResolvedValue([] as never);
    vi.mocked(engineApi.status).mockResolvedValue([] as never);
    vi.mocked(sampleApi.plan).mockResolvedValue({
      devices: [{ id: 4190001, name: "Sample AHU-1", role: "bbmd", object_count: 13, bind_address: "127.0.0.1/8:47808", port: 47808 }],
      refusal: null,
      notes: [],
    });
    vi.mocked(sampleApi.load).mockResolvedValue({ devices: [] });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const router = createMemoryRouter(
      [
        {
          path: "*",
          element: (
            <Routes>
              <Route path="/devices" element={<Dashboard />} />
              <Route path="/devices/sample" element={<Dashboard />} />
            </Routes>
          ),
        },
      ],
      { initialEntries: ["/devices/sample"] }
    );
    render(
      <QueryClientProvider client={client}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    );
    const panel = await screen.findByRole("dialog", { name: "Sample building" });
    await user.click(await within(panel).findByRole("button", { name: "Load" }));
    await vi.waitFor(() => expect(router.state.location.pathname).toBe("/devices"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows what the load did on the page once the panel has closed", async () => {
    const user = userEvent.setup();
    vi.mocked(devicesApi.list).mockResolvedValue([] as never);
    vi.mocked(engineApi.status).mockResolvedValue([] as never);
    vi.mocked(sampleApi.plan).mockResolvedValue({
      devices: [{ id: 4190001, name: "Sample AHU-1", role: "bbmd", object_count: 13, bind_address: "127.0.0.1/8:47808", port: 47808 }],
      refusal: null,
      notes: [],
    });
    vi.mocked(sampleApi.load).mockResolvedValue({
      devices: [
        { id: 4190001, name: "Sample AHU-1", bind_address: null, port: 47808, started: true, error: null },
        { id: 4190101, name: "Sample VAV-1", bind_address: null, port: 47809, started: false, error: "port in use" },
      ],
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const router = createMemoryRouter(
      [
        {
          path: "*",
          element: (
            <Routes>
              <Route path="/devices" element={<Dashboard />} />
              <Route path="/devices/sample" element={<Dashboard />} />
            </Routes>
          ),
        },
      ],
      { initialEntries: ["/devices/sample"] }
    );
    render(
      <QueryClientProvider client={client}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    );
    const panel = await screen.findByRole("dialog", { name: "Sample building" });
    await user.click(await within(panel).findByRole("button", { name: "Load" }));
    await vi.waitFor(() => expect(router.state.location.pathname).toBe("/devices"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(
      await screen.findByText("Loaded the sample building; started 1 of 2 devices. Sample VAV-1: port in use")
    ).toBeInTheDocument();
  });
});

describe("Dashboard no-login warning", () => {
  function renderDashboard(mode: "none" | "login", listenAddress: string | null) {
    vi.mocked(devicesApi.list).mockResolvedValue([] as never);
    vi.mocked(engineApi.status).mockResolvedValue([] as never);
    vi.mocked(authApi.state).mockResolvedValue({ mode, signed_in: mode === "login", username: null });
    vi.mocked(versionApi.get).mockResolvedValue({ version: "0.43.0", service: false, listen_address: listenAddress });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <Dashboard />
        </MemoryRouter>
      </QueryClientProvider>
    );
  }

  it("shows the no-login warning", async () => {
    renderDashboard("none", "0.0.0.0");
    expect(await screen.findByText(/BACpack is running without a login\./)).toBeInTheDocument();
  });

  it("links to the Security page", async () => {
    renderDashboard("none", "0.0.0.0");
    expect(await screen.findByRole("link", { name: "Turn one on under Security." })).toHaveAttribute(
      "href",
      "/settings/security"
    );
  });

  it("shows nothing in login mode", async () => {
    renderDashboard("login", "0.0.0.0");
    await screen.findByRole("heading", { level: 1, name: "Devices" });
    await vi.waitFor(() => expect(authApi.state).toHaveBeenCalled());
    expect(screen.queryByText(/running without a login/)).not.toBeInTheDocument();
  });
});
