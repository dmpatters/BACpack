import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Link, MemoryRouter, Route, RouterProvider, Routes, createMemoryRouter, useParams } from "react-router-dom";
import { parseId } from "../utils/ids";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi, beforeEach } from "vitest";
import ObjectDetail from "./ObjectDetail";
import { LeaveGuard, UnsavedChangesProvider } from "../components/UnsavedChanges";
import { devicesApi, liveApi, objectsApi, unitsApi } from "../api/endpoints";
import type { BacnetObjectDTO } from "../api/types";
import { ApiError } from "../api/http";

vi.mock("../api/endpoints", () => ({
  devicesApi: { get: vi.fn() },
  objectsApi: { list: vi.fn(), update: vi.fn(), remove: vi.fn() },
  unitsApi: { list: vi.fn() },
  trendApi: { range: vi.fn(), exportCsvUrl: vi.fn(() => "") },
  trendStorageApi: { get: vi.fn() },
  engineApi: { status: vi.fn().mockResolvedValue([]) },
  liveApi: { object: vi.fn().mockRejectedValue(new Error("not under test")) },
}));

// What DeviceDetail does with the route: parse the ids, pass the pieces.
function Page() {
  const { deviceId, objectId, tab = "general" } = useParams();
  return <ObjectDetail deviceId={Number(deviceId)} objectId={parseId(objectId)} tab={tab} />;
}

function obj(objectType: string): BacnetObjectDTO {
  return {
    id: 5,
    device_id: 599002,
    object_type: objectType,
    instance_number: 1,
    name: "OBJ-1",
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
    schedule_default: null,
    weekly_schedule: null,
    exception_schedule: null,
    schedule_effective_start: null,
    schedule_effective_end: null,
    schedule_write_targets: null,
    schedule_priority: 14,
    date_list: null,
    alarming_enabled: false,
    notification_class_instance: null,
    alarm_time_delay: 0,
    alarm_time_delay_normal: null,
    alarm_notify_type: "alarm",
    alarm_event_enable: null,
    alarm_high_limit: null,
    alarm_low_limit: null,
    alarm_deadband: 0,
    alarm_event_detection_enable: true,
    alarm_limit_enable: null,
    alarm_value: null,
    alarm_values: null,
    notification_priority: null,
    notification_ack_required: null,
    notification_recipient_list: null,
  } as BacnetObjectDTO;
}

function tabLinks() {
  return within(screen.getByRole("navigation", { name: "Object sections" })).getAllByRole("link");
}

async function findTabLinks() {
  await screen.findByRole("navigation", { name: "Object sections" });
  return tabLinks();
}

async function renderFor(objectType: string, path = "/devices/599002/objects/5") {
  vi.mocked(devicesApi.get).mockResolvedValue({ id: 599002, name: "Dev" } as never);
  vi.mocked(objectsApi.list).mockResolvedValue([obj(objectType)]);
  vi.mocked(unitsApi.list).mockResolvedValue({ units: [] } as never);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/devices/:deviceId/objects/:objectId/:tab?" element={<Page />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
  return findTabLinks();
}

describe("ObjectDetail live values", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows live values above the tabs of an object with a present value", async () => {
    await renderFor("analog-value");
    expect(screen.getByRole("region", { name: "Live values" })).toBeInTheDocument();
  });

  it("has no live values for a notification class", async () => {
    await renderFor("notification-class");
    expect(screen.queryByRole("region", { name: "Live values" })).not.toBeInTheDocument();
  });

  it("shows the priority array column for a commandable object only", async () => {
    vi.mocked(liveApi.object).mockResolvedValue({
      object_id: 5,
      object_type: "analog-output",
      instance: 1,
      present_value: 1,
      priority_array: Array(16).fill(null),
      controlling_priority: null,
      relinquish_default: 0,
      status_flags: [],
      event_state: "normal",
      reliability: null,
      out_of_service: false,
      read_at: "2026-10-16T10:48:02+00:00",
    });
    await renderFor("analog-output");
    expect(await screen.findByLabelText("Priority array", { selector: "aside" })).toBeInTheDocument();
  });

  it("has no priority array column for an input", async () => {
    await renderFor("analog-input");
    expect(screen.queryByLabelText("Priority array", { selector: "aside" })).not.toBeInTheDocument();
  });
});

describe("ObjectDetail tab visibility", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows General and Trending for a plain analog object, and no Alarming-only tabs it can't use", async () => {
    const tabs = (await renderFor("analog-input")).map((t) => t.textContent);
    expect(tabs).toContain("General");
    expect(tabs).toContain("Trending");
    expect(tabs).toContain("Alarming"); // analog-input is alarming-capable
    expect(tabs).not.toContain("Schedule");
  });

  it("hides Alarming for a type with no intrinsic reporting", async () => {
    const tabs = (await renderFor("binary-output")).map((t) => t.textContent);
    expect(tabs).not.toContain("Alarming");
  });

  it("says so for a tab the object doesn't have", async () => {
    vi.mocked(devicesApi.get).mockResolvedValue({ id: 599002, name: "Dev" } as never);
    vi.mocked(objectsApi.list).mockResolvedValue([obj("calendar")]);
    renderAt("/devices/599002/objects/5/alarming");
    expect(await screen.findByRole("heading", { name: 'No "alarming" tab' })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to OBJ-1" })).toHaveAttribute("href", "/devices/599002/objects/5");
  });

  it("opens the Recipients tab of a notification class from its URL", async () => {
    await renderFor("notification-class", "/devices/599002/objects/5/recipients");
    expect(screen.getByRole("link", { name: "Recipients" })).toHaveAttribute("aria-current", "page");
    expect(document.title).toBe("OBJ-1 · Recipients · Dev · BACpack");
  });

  it("has no Trending tab for a notification class", async () => {
    const tabs = (await renderFor("notification-class")).map((t) => t.textContent);
    expect(tabs).not.toContain("Trending");
    expect(tabs).toContain("Recipients");
  });

  it("says there is no trending tab when a notification class is opened at /trending", async () => {
    await renderFor("notification-class", "/devices/599002/objects/5/trending");
    expect(await screen.findByRole("heading", { name: 'No "trending" tab' })).toBeInTheDocument();
  });

  it("shows Schedule for a schedule", async () => {
    const tabs = (await renderFor("schedule")).map((t) => t.textContent);
    expect(tabs).toContain("Schedule");
  });

  it("gives a calendar a Dates tab and no Schedule tab", async () => {
    const tabs = (await renderFor("calendar", "/devices/599002/objects/5/dates")).map((t) => t.textContent);
    expect(tabs).toContain("Dates");
    expect(tabs).not.toContain("Schedule");
    expect(screen.getByRole("link", { name: "Dates" })).toHaveAttribute("aria-current", "page");
    expect(await screen.findByText(/No dates/, { selector: "td" })).toBeInTheDocument();
    expect(document.title).toBe("OBJ-1 · Dates · Dev · BACpack");
  });
});

function renderAt(path: string) {
  vi.mocked(unitsApi.list).mockResolvedValue({ units: [] } as never);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/devices/:deviceId/objects/:objectId/:tab?" element={<Page />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

// A stale bookmark must not render a blank page.
describe("ObjectDetail with a bad link", () => {
  beforeEach(() => vi.clearAllMocks());

  it("says the object wasn't found", async () => {
    vi.mocked(devicesApi.get).mockResolvedValue({ id: 599002, name: "Dev" } as never);
    vi.mocked(objectsApi.list).mockResolvedValue([obj("analog-value")]);
    renderAt("/devices/599002/objects/99");
    expect(await screen.findByRole("heading", { name: "Object 99 not found" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to Dev" })).toHaveAttribute("href", "/devices/599002");
  });

  it("says the device wasn't found on a 404", async () => {
    vi.mocked(devicesApi.get).mockRejectedValue(new ApiError("Device 1 not found", 404));
    vi.mocked(objectsApi.list).mockRejectedValue(new ApiError("Device 1 not found", 404));
    renderAt("/devices/1/objects/5");
    expect(await screen.findByRole("heading", { name: "Device 1 not found" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to devices" })).toHaveAttribute("href", "/");
  });

  it("says why when the device can't be loaded for another reason", async () => {
    vi.mocked(devicesApi.get).mockRejectedValue(new Error("Failed to fetch"));
    vi.mocked(objectsApi.list).mockRejectedValue(new Error("Failed to fetch"));
    renderAt("/devices/1/objects/5");
    expect(await screen.findByRole("heading", { name: "Couldn't load object 5" })).toBeInTheDocument();
    expect(screen.getByText("Failed to fetch")).toBeInTheDocument();
  });

  it("says an object id that isn't a number isn't found", async () => {
    vi.mocked(devicesApi.get).mockResolvedValue({ id: 599002, name: "Dev" } as never);
    vi.mocked(objectsApi.list).mockResolvedValue([obj("analog-value")]);
    renderAt("/devices/599002/objects/abc");
    expect(await screen.findByRole("heading", { name: "Object not found" })).toBeInTheDocument();
  });

  it("titles each problem with its heading", async () => {
    vi.mocked(devicesApi.get).mockResolvedValue({ id: 599002, name: "Dev" } as never);
    vi.mocked(objectsApi.list).mockResolvedValue([obj("analog-value")]);
    renderAt("/devices/599002/objects/99");
    await screen.findByRole("heading", { name: "Object 99 not found" });
    expect(document.title).toBe("Object 99 not found · BACpack");
  });

  it("titles a failed device load with its heading, never bare BACpack", async () => {
    vi.mocked(devicesApi.get).mockRejectedValue(new Error("Failed to fetch"));
    vi.mocked(objectsApi.list).mockRejectedValue(new Error("Failed to fetch"));
    renderAt("/devices/1/objects/5");
    await screen.findByRole("heading", { name: "Couldn't load object 5" });
    expect(document.title).toBe("Couldn't load object 5 · BACpack");
  });
});

// A failed refetch (uvicorn restarting) must not replace a loaded page,
// and its unsaved edits, with "not found".
describe("ObjectDetail after a failed refetch", () => {
  it("keeps the loaded object on screen", async () => {
    vi.mocked(devicesApi.get).mockResolvedValue({ id: 599002, name: "Dev" } as never);
    vi.mocked(objectsApi.list).mockResolvedValue([obj("analog-value")]);
    vi.mocked(unitsApi.list).mockResolvedValue({ units: [] } as never);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/devices/599002/objects/5"]}>
          <Routes>
            <Route path="/devices/:deviceId/objects/:objectId/:tab?" element={<Page />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );
    await findTabLinks();
    vi.mocked(objectsApi.list).mockRejectedValue(new Error("Failed to fetch"));
    await act(() => client.invalidateQueries({ queryKey: ["objects", 599002] }));
    // Let the observers re-render with the error (TanStack notifies on a later tick).
    await act(() => new Promise((resolve) => setTimeout(resolve, 50)));
    expect(client.getQueryState(["objects", 599002])?.status).toBe("error");
    expect(screen.queryByRole("heading", { name: /not found|Couldn't load/ })).not.toBeInTheDocument();
    expect(tabLinks().length).toBeGreaterThan(0);
  });

  it("keeps unsaved General edits across a switch to Trending and back", async () => {
    await renderFor("analog-value");
    const user = userEvent.setup();
    const description = await screen.findByLabelText("Description");
    await user.type(description, "not saved yet");
    await user.click(screen.getByRole("link", { name: "Trending" }));
    await user.click(screen.getByRole("link", { name: "General" }));
    expect(screen.getByLabelText("Description")).toHaveValue("not saved yet");
  });

  it("keeps unsaved edits through a mistyped tab URL", async () => {
    vi.mocked(devicesApi.get).mockResolvedValue({ id: 599002, name: "Dev" } as never);
    vi.mocked(objectsApi.list).mockResolvedValue([obj("analog-value")]);
    vi.mocked(unitsApi.list).mockResolvedValue({ units: [] } as never);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/devices/599002/objects/5"]}>
          <Link to="/devices/599002/objects/5/bogus">mistyped</Link>
          <Routes>
            <Route path="/devices/:deviceId/objects/:objectId/:tab?" element={<Page />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Description"), "not saved yet");
    await user.click(screen.getByRole("link", { name: "mistyped" }));
    expect(await screen.findByRole("heading", { name: 'No "bogus" tab' })).toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "Back to OBJ-1" }));
    expect(screen.getByLabelText("Description")).toHaveValue("not saved yet");
  });
});

describe("ObjectDetail unsaved-changes guard", () => {
  beforeEach(() => vi.clearAllMocks());

  it("keeps an edit through a tab switch, and asks before the nav drops it", async () => {
    vi.mocked(devicesApi.get).mockResolvedValue({ id: 599002, name: "Dev" } as never);
    vi.mocked(objectsApi.list).mockResolvedValue([obj("analog-value")]);
    vi.mocked(unitsApi.list).mockResolvedValue({ units: [] } as never);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const router = createMemoryRouter(
      [
        {
          path: "*",
          element: (
            <UnsavedChangesProvider>
              <Routes>
                <Route path="/devices/:deviceId/objects/:objectId/:tab?" element={<Page />} />
                <Route path="*" element={<p>somewhere else</p>} />
              </Routes>
              <LeaveGuard />
            </UnsavedChangesProvider>
          ),
        },
      ],
      { initialEntries: ["/devices/599002/objects/5"] }
    );
    render(
      <QueryClientProvider client={client}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    );
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Description"), "edited");

    await user.click(screen.getByRole("link", { name: "Trending" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/devices/599002/objects/5/trending");
    await user.click(screen.getByRole("link", { name: "General" }));
    expect(screen.getByLabelText("Description")).toHaveValue("edited");

    await act(() => router.navigate("/logs"));
    expect(screen.getByRole("alertdialog", { name: "Discard unsaved changes?" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/devices/599002/objects/5");
  });

  it("shows a loading line while the object loads", async () => {
    vi.mocked(devicesApi.get).mockReturnValue(new Promise(() => {}));
    vi.mocked(objectsApi.list).mockReturnValue(new Promise(() => {}));
    vi.mocked(unitsApi.list).mockResolvedValue({ units: [] } as never);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/devices/599002/objects/5"]}>
          <Routes>
            <Route path="/devices/:deviceId/objects/:objectId/:tab?" element={<Page />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );
    expect(await screen.findByRole("status")).toHaveTextContent("Loading object…");
  });
});
