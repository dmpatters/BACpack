import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ImportEdePanel from "./ImportEdePanel";
import { PlanChangedError, edeApi } from "../api/endpoints";
import { ApiError } from "../api/http";
import type { EdeDeviceChoice, EdeDevicePlan, EdePlanResult, EdeReadResult } from "../api/types";

vi.mock("../api/endpoints", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/endpoints")>();
  return {
    PlanChangedError: actual.PlanChangedError,
    edeApi: { read: vi.fn(), plan: vi.fn(), create: vi.fn() },
    versionApi: { get: vi.fn().mockResolvedValue({ version: "0.39.0", service: false }) },
  };
});

const twoDevices: EdeReadResult = {
  project_name: "Plant",
  version_of_layout: "2.3",
  columns_not_used: ["min-present-value"],
  file_notes: [],
  devices: [
    { instance: 1000, name: "Boiler", object_count: 12, existing_device: null },
    { instance: 2000, name: "Chiller", object_count: 3, existing_device: { id: 2000, name: "Old chiller" } },
  ],
};

function devicePlan(choice: EdeDeviceChoice, overrides: Partial<EdeDevicePlan> = {}): EdeDevicePlan {
  return {
    instance: choice.instance,
    device: { id: choice.id, name: choice.name, description: null },
    object_count: 2,
    counts: { "analog-input": 2 },
    skipped: [],
    notes: [],
    skipped_types: {},
    problem: null,
    ...overrides,
  };
}

// A plan for whatever was ticked, with an optional change per instance.
function planFor(change: Record<number, Partial<EdeDevicePlan>> = {}, digest = "d1") {
  vi.mocked(edeApi.plan).mockImplementation(
    async (_file, _texts, choices): Promise<EdePlanResult> => ({
      plans: choices.map((c) => devicePlan(c, change[c.instance])),
      vendor_name: null,
      plan_digest: digest,
    })
  );
}

const edeFile = new File(["#keyname\n"], "plant.csv");

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/devices/import-ede"]}>
        <Routes>
          <Route path="/devices/import-ede" element={<ImportEdePanel open onClose={() => {}} />} />
          <Route path="/devices/:id" element={<p>device page</p>} />
          <Route path="/devices" element={<p>devices page</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

async function chooseFile(user: ReturnType<typeof userEvent.setup>, read: EdeReadResult = twoDevices) {
  vi.mocked(edeApi.read).mockResolvedValue(read);
  await user.upload(screen.getByLabelText("EDE file"), edeFile);
}

describe("ImportEdePanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    planFor();
  });

  it("reads the file on choice and lists its devices, all ticked", async () => {
    const user = userEvent.setup();
    renderPanel();
    await chooseFile(user);
    expect(edeApi.read).toHaveBeenCalledWith(edeFile, null);
    expect(await screen.findByRole("checkbox", { name: "Create device 1000" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Create device 2000" })).toBeChecked();
    expect(screen.getByLabelText("Name for device 1000")).toHaveValue("Boiler");
    expect(screen.getByText(/Columns not used/)).toBeInTheDocument();
    expect(await screen.findByRole("group", { name: "Device 1000 Boiler" })).toBeInTheDocument();
  });

  it("sends the state-texts file with the read and the plan", async () => {
    const user = userEvent.setup();
    renderPanel();
    const texts = new File(["1;Off;On\n"], "states.csv");
    await user.upload(screen.getByLabelText("State texts file"), texts);
    await chooseFile(user);
    await screen.findByRole("group", { name: "Device 1000 Boiler" });
    expect(edeApi.read).toHaveBeenLastCalledWith(edeFile, texts);
    expect(vi.mocked(edeApi.plan).mock.lastCall?.[1]).toBe(texts);
  });

  it("an existing instance shows the BACpack device and its id and name can be changed", async () => {
    const user = userEvent.setup();
    renderPanel();
    await chooseFile(user);
    expect(await screen.findByText(/BACpack already has device 2000 \(Old chiller\)/)).toBeInTheDocument();
    const id = screen.getByLabelText("Instance for device 2000");
    await user.clear(id);
    await user.type(id, "2001");
    await user.clear(screen.getByLabelText("Name for device 2000"));
    await user.type(screen.getByLabelText("Name for device 2000"), "Chiller 2");
    expect(await screen.findByRole("group", { name: "Device 2001 Chiller 2" })).toBeInTheDocument();
    expect(vi.mocked(edeApi.plan).mock.lastCall?.[2]).toEqual([
      { instance: 1000, id: 1000, name: "Boiler" },
      { instance: 2000, id: 2001, name: "Chiller 2" },
    ]);
  });

  it("says once that ticked devices share the default address, only with more than one", async () => {
    const user = userEvent.setup();
    renderPanel();
    await chooseFile(user);
    const note = /Give each its own port in Process/;
    expect(await screen.findByText(note)).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "Create device 2000" }));
    expect(screen.queryByText(note)).not.toBeInTheDocument();
  });

  it("an unticked device drops out of the preview and the create", async () => {
    const user = userEvent.setup();
    vi.mocked(edeApi.create).mockResolvedValue({ device_ids: [1000] });
    renderPanel();
    await chooseFile(user);
    await screen.findByRole("group", { name: "Device 2000 Chiller" });
    await user.click(screen.getByRole("checkbox", { name: "Create device 2000" }));
    await waitFor(() => expect(screen.queryByRole("group", { name: "Device 2000 Chiller" })).not.toBeInTheDocument());
    await screen.findByRole("group", { name: "Device 1000 Boiler" });
    await user.click(screen.getByRole("button", { name: "Create device" }));
    expect(edeApi.create).toHaveBeenCalledWith(edeFile, null, [{ instance: 1000, id: 1000, name: "Boiler" }], "d1");
  });

  it("lists skipped objects and notes, with the request link only for skipped types", async () => {
    const user = userEvent.setup();
    planFor({
      1000: {
        skipped: [{ object_type: "event-enrollment", instance: 1, name: "Loop", reason: "not supported", code: "unsupported-type" }],
        notes: [{ object_type: "analog-input", instance: 3, name: "Temp", note: "units '---' dropped" }],
        skipped_types: { "event-enrollment (9)": 1 },
      },
      2000: {
        skipped: [{ object_type: "analog-input", instance: 9, name: "Bad", reason: "instance out of range", code: "invalid" }],
      },
    });
    renderPanel();
    await chooseFile(user);
    const boiler = await screen.findByRole("group", { name: "Device 1000 Boiler" });
    expect(within(boiler).getByText(/Not imported \(1\)/)).toBeInTheDocument();
    expect(within(boiler).getByText(/units '---' dropped/)).toBeInTheDocument();
    const link = within(boiler).getByRole("link", { name: "Request support for these types" });
    expect(new URL(link.getAttribute("href")!).searchParams.get("title")).toContain("EDE imports");
    const chiller = screen.getByRole("group", { name: "Device 2000 Chiller" });
    expect(within(chiller).getByText(/instance out of range/)).toBeInTheDocument();
    expect(within(chiller).queryByRole("link", { name: "Request support for these types" })).not.toBeInTheDocument();
  });

  it("a plan problem shows and holds Create back", async () => {
    const user = userEvent.setup();
    planFor({ 2000: { problem: "device 2000 already exists" } });
    renderPanel();
    await chooseFile(user);
    expect(await screen.findByText(/device 2000 already exists\. Change the instance or name/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create devices" })).toBeDisabled();
  });

  it("creates the ticked devices with the preview's digest and opens the only one", async () => {
    const user = userEvent.setup();
    vi.mocked(edeApi.create).mockResolvedValue({ device_ids: [1000] });
    renderPanel();
    await chooseFile(user, { ...twoDevices, devices: [twoDevices.devices[0]] });
    await screen.findByRole("group", { name: "Device 1000 Boiler" });
    await user.click(screen.getByRole("button", { name: "Create device" }));
    expect(edeApi.create).toHaveBeenCalledWith(edeFile, null, [{ instance: 1000, id: 1000, name: "Boiler" }], "d1");
    expect(await screen.findByText("device page")).toBeInTheDocument();
  });

  it("goes back to the device list after creating several", async () => {
    const user = userEvent.setup();
    vi.mocked(edeApi.create).mockResolvedValue({ device_ids: [1000, 2001] });
    renderPanel();
    await chooseFile(user);
    await screen.findByRole("group", { name: "Device 2000 Chiller" });
    await user.click(screen.getByRole("button", { name: "Create devices" }));
    expect(await screen.findByText("devices page")).toBeInTheDocument();
  });

  it("a plan-changed create re-plans and says nothing was created", async () => {
    const user = userEvent.setup();
    vi.mocked(edeApi.create).mockRejectedValueOnce(new PlanChangedError("changed since the preview"));
    renderPanel();
    await chooseFile(user);
    await screen.findByRole("group", { name: "Device 2000 Chiller" });
    const plans = vi.mocked(edeApi.plan).mock.calls.length;
    planFor({}, "d2");
    await user.click(screen.getByRole("button", { name: "Create devices" }));
    expect(await screen.findByText(/Nothing was created/)).toBeInTheDocument();
    expect(vi.mocked(edeApi.plan).mock.calls.length).toBe(plans + 1);
    vi.mocked(edeApi.create).mockResolvedValueOnce({ device_ids: [1000, 2000] });
    await user.click(screen.getByRole("button", { name: "Create devices" }));
    expect(vi.mocked(edeApi.create).mock.lastCall?.[3]).toBe("d2");
  });

  it("shows another create refusal as it is", async () => {
    const user = userEvent.setup();
    vi.mocked(edeApi.create).mockRejectedValue(new ApiError("device 1000 (Boiler): a device named 'Boiler' already exists", 409));
    renderPanel();
    await chooseFile(user);
    await screen.findByRole("group", { name: "Device 2000 Chiller" });
    await user.click(screen.getByRole("button", { name: "Create devices" }));
    expect(await screen.findByText(/a device named 'Boiler' already exists/)).toBeInTheDocument();
  });

  it("shows a file the server refuses", async () => {
    const user = userEvent.setup();
    vi.mocked(edeApi.read).mockRejectedValue(new ApiError("not an EDE file: no keyname line", 400));
    renderPanel();
    await user.upload(screen.getByLabelText("EDE file"), edeFile);
    expect(await screen.findByText(/not an EDE file/)).toBeInTheDocument();
    expect(edeApi.plan).not.toHaveBeenCalled();
  });
});
