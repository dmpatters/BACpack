// The unsaved-changes guard on the device page, with the real Settings tab
// (DeviceDetail.test.tsx stubs every tab) under a data router.
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, RouterProvider, Routes, createMemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import DeviceDetail from "./DeviceDetail";
import { LeaveGuard, UnsavedChangesProvider } from "../components/UnsavedChanges";
import { devicesApi, engineApi } from "../api/endpoints";
import type { DeviceSummary } from "../api/types";

vi.mock("../api/endpoints", () => ({
  devicesApi: { get: vi.fn(), remove: vi.fn(), update: vi.fn() },
  engineApi: { status: vi.fn(), start: vi.fn(), stop: vi.fn(), restart: vi.fn() },
}));
vi.mock("../components/DeviceObjectsTab", () => ({ default: () => <p>objects panel</p> }));
vi.mock("../components/DeviceProcessTab", () => ({ default: () => <p>process panel</p> }));
vi.mock("../components/DeviceTrafficTab", () => ({ default: () => <p>traffic panel</p> }));
vi.mock("../components/DeviceLogsTab", () => ({ default: () => <p>logs panel</p> }));
vi.mock("../components/DeviceImportExportTab", () => ({ default: () => <p>import-export panel</p> }));
vi.mock("../components/DeviceClientTab", () => ({ default: () => <p>client panel</p> }));

const DEVICE = {
  id: 599002,
  name: "Bugfix Session Device",
  description: "",
  location: null,
  vendor_identifier: 999,
  vendor_name: null,
  object_count: 0,
  bind_address: null,
} as DeviceSummary;

function renderGuardedDevice(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter(
    [
      {
        path: "*",
        element: (
          <UnsavedChangesProvider>
            <Routes>
              <Route path="/devices/:deviceId/:tab?" element={<DeviceDetail />} />
              <Route path="*" element={<p>device list</p>} />
            </Routes>
            <LeaveGuard />
          </UnsavedChangesProvider>
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

describe("DeviceDetail unsaved-changes guard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(devicesApi.get).mockResolvedValue(DEVICE);
    vi.mocked(engineApi.status).mockResolvedValue([] as never);
  });

  it("asks once, in the delete dialog, when deleting with an unsaved Settings edit", async () => {
    vi.mocked(devicesApi.remove).mockResolvedValue(undefined as never);
    const user = userEvent.setup();
    const router = renderGuardedDevice("/devices/599002/settings");
    await user.type(await screen.findByLabelText("Description"), " edited");
    await user.click(screen.getByRole("button", { name: "Delete device" }));
    const dialog = screen.getByRole("alertdialog", { name: "Delete device 599002?" });
    await user.click(within(dialog).getByRole("button", { name: "Delete device" }));
    await screen.findByText("device list");
    expect(screen.queryByRole("alertdialog", { name: "Discard unsaved changes?" })).not.toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/");
  });

  it("asks before the nav drops an unsaved Settings edit", async () => {
    const user = userEvent.setup();
    const router = renderGuardedDevice("/devices/599002/settings");
    await user.type(await screen.findByLabelText("Description"), " edited");
    await act(() => router.navigate("/logs"));
    expect(screen.getByRole("alertdialog", { name: "Discard unsaved changes?" })).toBeInTheDocument();
  });
});
