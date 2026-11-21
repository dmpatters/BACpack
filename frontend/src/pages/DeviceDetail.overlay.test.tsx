// An object's URL is the device page with that object's overlay on top: the
// real Objects tab and the real object page, under a data router and the
// unsaved-changes guard.
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, RouterProvider, Routes, createMemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import DeviceDetail from "./DeviceDetail";
import { LeaveGuard, UnsavedChangesProvider } from "../components/UnsavedChanges";
import { devicesApi, engineApi, objectsApi, unitsApi } from "../api/endpoints";
import type { BacnetObjectDTO, DeviceSummary } from "../api/types";

vi.mock("../api/endpoints", () => ({
  devicesApi: { get: vi.fn(), remove: vi.fn(), update: vi.fn() },
  engineApi: { status: vi.fn(), start: vi.fn(), stop: vi.fn(), restart: vi.fn() },
  objectsApi: { list: vi.fn(), update: vi.fn(), remove: vi.fn(), create: vi.fn(), batchUpdate: vi.fn(), batchDelete: vi.fn() },
  unitsApi: { list: vi.fn() },
  trendApi: { range: vi.fn(), exportCsvUrl: vi.fn(() => "") },
  trendStorageApi: { get: vi.fn() },
  liveApi: { object: vi.fn().mockRejectedValue(new Error("not under test")) },
}));
vi.mock("../components/DeviceProcessTab", () => ({ default: () => <p>process panel</p> }));
vi.mock("../components/DeviceTrafficTab", () => ({ default: () => <p>traffic panel</p> }));
vi.mock("../components/DeviceLogsTab", () => ({ default: () => <p>logs panel</p> }));
vi.mock("../components/DeviceImportExportTab", () => ({ default: () => <p>import-export panel</p> }));
vi.mock("../components/DeviceSettingsTab", () => ({ default: () => <p>settings panel</p> }));
vi.mock("../components/DeviceClientTab", () => ({ default: () => <p>client panel</p> }));

const DEVICE = {
  id: 1,
  name: "Test Device",
  description: "",
  location: null,
  vendor_identifier: 999,
  vendor_name: null,
  object_count: 2,
  bind_address: null,
} as DeviceSummary;

function makeObject(id: number, name: string, instance: number): BacnetObjectDTO {
  return {
    id,
    device_id: 1,
    object_type: "analog-value",
    instance_number: instance,
    name,
    description: "",
    units: null,
    bacnet_writable: true,
    out_of_service_default: false,
    relinquish_default: null,
    state_text: null,
    trended: false,
    trend_mode: "interval",
    trend_interval_seconds: null,
    trend_sample_cap: null,
    schedule_value_type: null,
    alarming_enabled: false,
    alarm_time_delay: 0,
    alarm_notify_type: "alarm",
    alarm_deadband: 0,
    alarm_event_detection_enable: true,
  } as unknown as BacnetObjectDTO;
}

const OBJECTS = [makeObject(5, "AV-A", 1), makeObject(6, "AV-B", 2)];

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter(
    [
      {
        path: "*",
        element: (
          <UnsavedChangesProvider>
            <Routes>
              <Route path="/devices/:deviceId/:tab?" element={<DeviceDetail />} />
              <Route path="/devices/:deviceId/objects/:objectId/:tab?" element={<DeviceDetail />} />
              <Route path="*" element={<p>elsewhere</p>} />
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

async function openRow(user: ReturnType<typeof userEvent.setup>, name: string) {
  const row = (await screen.findByText(name, { selector: "td" })).closest("tr")!;
  await user.click(within(row).getByRole("link", { name: "Edit" }));
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(devicesApi.get).mockResolvedValue(DEVICE);
  vi.mocked(engineApi.status).mockResolvedValue([] as never);
  vi.mocked(objectsApi.list).mockResolvedValue(OBJECTS);
  vi.mocked(unitsApi.list).mockResolvedValue({ units: [] } as never);
});

describe("an object's overlay", () => {
  it("opens from a row's Edit at the object's URL, with the list still there", async () => {
    const user = userEvent.setup();
    const router = renderAt("/devices/1");
    await openRow(user, "AV-A");
    expect(router.state.location.pathname).toBe("/devices/1/objects/5");
    expect(await screen.findByRole("dialog", { name: "AV-A" })).toBeInTheDocument();
    expect(screen.getByText("AV-B", { selector: "td" })).toBeInTheDocument();
  });

  it("names the device and the object in its top bar, and falls back to the id when the object isn't found", async () => {
    const user = userEvent.setup();
    renderAt("/devices/1");
    await openRow(user, "AV-A");
    await screen.findByRole("dialog", { name: "AV-A" });
    expect(document.querySelector(".bp-overlay__bar")).toHaveTextContent("Test Device › AV-A");
    await user.keyboard("{Escape}");

    renderAt("/devices/1/objects/99");
    await vi.waitFor(() =>
      expect(document.querySelector(".bp-overlay__bar")).toHaveTextContent("Test Device › Object 99")
    );
  });

  it("closes with the x, back to the list", async () => {
    const user = userEvent.setup();
    const router = renderAt("/devices/1");
    await openRow(user, "AV-A");
    const dialog = await screen.findByRole("dialog", { name: "AV-A" });
    await user.click(within(dialog).getByRole("button", { name: "Close" }));
    expect(router.state.location.pathname).toBe("/devices/1");
    expect(screen.queryByRole("dialog", { name: "AV-A" })).not.toBeInTheDocument();
  });

  it("closes with Escape", async () => {
    const user = userEvent.setup();
    const router = renderAt("/devices/1");
    await openRow(user, "AV-A");
    await screen.findByRole("dialog", { name: "AV-A" });
    await user.keyboard("{Escape}");
    expect(router.state.location.pathname).toBe("/devices/1");
    expect(screen.queryByRole("dialog", { name: "AV-A" })).not.toBeInTheDocument();
  });

  it("closes with the browser's Back", async () => {
    const user = userEvent.setup();
    const router = renderAt("/devices/1");
    await openRow(user, "AV-A");
    await screen.findByRole("dialog", { name: "AV-A" });
    await act(() => router.navigate(-1));
    expect(router.state.location.pathname).toBe("/devices/1");
    expect(screen.queryByRole("dialog", { name: "AV-A" })).not.toBeInTheDocument();
  });

  it("leaves the list as it was: search, selection and a half-typed Add object", async () => {
    const user = userEvent.setup();
    renderAt("/devices/1");
    await screen.findByText("AV-A", { selector: "td" });
    await user.type(screen.getByRole("searchbox", { name: "Search objects" }), "AV");
    await user.click(screen.getByLabelText("Select AV-B"));
    await user.click(screen.getByRole("button", { name: "Add object" }));
    const add = screen.getByRole("form", { name: "Add object" });
    await user.type(within(add).getByLabelText("Name"), "Half typed");

    await openRow(user, "AV-A");
    const dialog = await screen.findByRole("dialog", { name: "AV-A" });
    await user.click(within(dialog).getByRole("button", { name: "Close" }));

    expect(screen.getByRole("searchbox", { name: "Search objects" })).toHaveValue("AV");
    expect(screen.getByLabelText("Select AV-B")).toBeChecked();
    expect(within(screen.getByRole("form", { name: "Add object" })).getByLabelText("Name")).toHaveValue("Half typed");
  });

  it("replaces the URL on a tab click, so Back still closes the overlay", async () => {
    const user = userEvent.setup();
    const router = renderAt("/devices/1");
    await openRow(user, "AV-A");
    const dialog = await screen.findByRole("dialog", { name: "AV-A" });
    await user.click(within(dialog).getByRole("link", { name: "Trending" }));
    expect(router.state.location.pathname).toBe("/devices/1/objects/5/trending");
    expect(router.state.historyAction).toBe("REPLACE");
    await act(() => router.navigate(-1));
    expect(router.state.location.pathname).toBe("/devices/1");
    expect(screen.queryByRole("dialog", { name: "AV-A" })).not.toBeInTheDocument();
  });

  it("opens from a link: the list under it, the named tab shown, and x replaces to the list", async () => {
    const user = userEvent.setup();
    const router = renderAt("/devices/1/objects/5/trending");
    const dialog = await screen.findByRole("dialog", { name: "AV-A" });
    expect(screen.getByText("AV-B", { selector: "td" })).toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: "Trending" })).toHaveAttribute("aria-current", "page");
    await user.click(within(dialog).getByRole("button", { name: "Close" }));
    expect(router.state.location.pathname).toBe("/devices/1");
    expect(router.state.historyAction).toBe("REPLACE");
  });

  it("says an object that no longer exists isn't found, over the list; x goes to the list", async () => {
    const user = userEvent.setup();
    const router = renderAt("/devices/1/objects/99");
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByRole("heading", { name: "Object 99 not found" })).toBeInTheDocument();
    expect(dialog).toHaveAccessibleName("Object 99");
    expect(screen.getByText("AV-B", { selector: "td" })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Close" }));
    expect(router.state.location.pathname).toBe("/devices/1");
  });

  it("leaves the not-found overlay by replacing, so Back doesn't reopen it", async () => {
    const user = userEvent.setup();
    const router = renderAt("/devices/1/objects/99");
    const dialog = await screen.findByRole("dialog");
    await user.click(await within(dialog).findByRole("link", { name: "Back to Test Device" }));
    expect(router.state.location.pathname).toBe("/devices/1");
    expect(router.state.historyAction).toBe("REPLACE");
  });

  it("asks before Back drops an unsaved edit; Cancel keeps the overlay, Discard closes it", async () => {
    const user = userEvent.setup();
    const router = renderAt("/devices/1");
    await openRow(user, "AV-A");
    const dialog = await screen.findByRole("dialog", { name: "AV-A" });
    await user.type(within(dialog).getByLabelText("Description"), "edited");

    await act(() => router.navigate(-1));
    expect(screen.getByRole("alertdialog", { name: "Discard unsaved changes?" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(router.state.location.pathname).toBe("/devices/1/objects/5");
    expect(within(screen.getByRole("dialog", { name: "AV-A" })).getByLabelText("Description")).toHaveValue("edited");

    await act(() => router.navigate(-1));
    await user.click(screen.getByRole("button", { name: "Discard changes" }));
    expect(router.state.location.pathname).toBe("/devices/1");
    expect(screen.queryByRole("dialog", { name: "AV-A" })).not.toBeInTheDocument();
  });

  it("titles the page with the object while open and with the device after", async () => {
    const user = userEvent.setup();
    renderAt("/devices/1");
    await screen.findByText("AV-A", { selector: "td" });
    expect(document.title).toBe("Test Device · Objects · BACpack");
    await openRow(user, "AV-A");
    const dialog = await screen.findByRole("dialog", { name: "AV-A" });
    expect(document.title).toBe("AV-A · General · Test Device · BACpack");
    await user.click(within(dialog).getByRole("button", { name: "Close" }));
    expect(document.title).toBe("Test Device · Objects · BACpack");
  });
});
