import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ClonePanel from "./ClonePanel";
import { cloneApi } from "../api/endpoints";
import { ApiError } from "../api/http";
import type { CloneJob, ClonePlanResult } from "../api/types";
import { BIND_ADDRESS_HELP, BIND_ADDRESS_PLACEHOLDER } from "../utils/bind";

vi.mock("../api/endpoints", () => ({
  cloneApi: { get: vi.fn(), cancel: vi.fn(), discard: vi.fn(), plan: vi.fn(), create: vi.fn() },
  versionApi: { get: vi.fn().mockResolvedValue({ version: "0.38.0", service: false }) },
}));

const running: CloneJob = {
  job_id: 7,
  status: "running",
  progress: { phase: "objects", done: 1240, total: 5000 },
  error: null,
  created_at: "2026-11-01T12:00:00+00:00",
  instance: 4001,
  address: null,
};
const done: CloneJob = { ...running, status: "done", progress: { phase: "objects", done: 5000, total: 5000 } };

function planResult(overrides: Partial<ClonePlanResult["plan"]> = {}): ClonePlanResult {
  return {
    plan: {
      device: { id: 4001, name: "Boiler Room", description: null, location: null, vendor_name: "Acme", vendor_identifier: 42 },
      vendor_name: "Acme",
      object_count: 3,
      counts: { "analog-input": 2, "binary-value": 1 },
      skipped: [{ object_type: "accumulator", instance: 9, name: "kWh", reason: "accumulator objects are not supported", code: "unsupported-type" }],
      notes: [{ object_type: "analog-value", instance: 2, name: "Setpoint", note: "not commandable on the controller" }],
      skipped_types: { accumulator: 1 },
      problem: null,
      ...overrides,
    },
    suggested_bind: { bind_address: "192.168.1.50/24:47809", port: 47809 },
  };
}

function renderPanel(onClose = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/devices/599001/client"]}>
        <Routes>
          <Route path="/devices/599001/client" element={<ClonePanel deviceId={599001} open onClose={onClose} />} />
          <Route path="/devices/:id" element={<p>new device page</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
  return onClose;
}

describe("ClonePanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(cloneApi.plan).mockResolvedValue(planResult());
  });

  it("shows progress and cancels", async () => {
    const user = userEvent.setup();
    vi.mocked(cloneApi.get).mockResolvedValue(running);
    vi.mocked(cloneApi.cancel).mockResolvedValue({ job_id: 7, status: "running", cancel_requested: true });
    renderPanel();
    expect(await screen.findByText("Reading 1,240 of 5,000 objects")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(cloneApi.cancel).toHaveBeenCalledWith(599001, 7);
  });

  it("labels the object-list phase", async () => {
    vi.mocked(cloneApi.get).mockResolvedValue({ ...running, progress: { phase: "object-list", done: 10, total: 400 } });
    renderPanel();
    expect(await screen.findByText("Reading the object list: 10 of 400")).toBeInTheDocument();
  });

  it("states the bind address format in the shared hint and placeholder", async () => {
    vi.mocked(cloneApi.get).mockResolvedValue(done);
    renderPanel();
    const field = await screen.findByLabelText("Bind address");
    expect(field).toHaveAttribute("placeholder", BIND_ADDRESS_PLACEHOLDER);
    expect(field).toHaveAccessibleDescription(`${BIND_ADDRESS_HELP} Blank uses the default.`);
  });

  it("pre-fills the controller's id and name and sends edited values to create", async () => {
    const user = userEvent.setup();
    vi.mocked(cloneApi.get).mockResolvedValue(done);
    vi.mocked(cloneApi.create).mockResolvedValue({ device_id: 4002 });
    renderPanel();
    const id = await screen.findByLabelText("Device instance");
    await waitFor(() => expect(id).toHaveValue(4001));
    expect(screen.getByLabelText("Device name")).toHaveValue("Boiler Room");
    expect(screen.getByLabelText("Bind address")).toHaveValue("192.168.1.50/24:47809");
    await user.clear(id);
    await user.type(id, "4002");
    await user.click(screen.getByRole("button", { name: "Create device" }));
    expect(cloneApi.create).toHaveBeenCalledWith(599001, 7, {
      id: 4002,
      name: "Boiler Room",
      bind_address: "192.168.1.50/24:47809",
    });
    expect(await screen.findByText("new device page")).toBeInTheDocument();
  });

  it("shows counts, skipped objects with reasons, and notes", async () => {
    vi.mocked(cloneApi.get).mockResolvedValue(done);
    renderPanel();
    expect(await screen.findByText("analog-input")).toBeInTheDocument();
    expect(screen.getByText(/kWh/)).toBeInTheDocument();
    expect(screen.getByText(/accumulator objects are not supported/)).toBeInTheDocument();
    expect(screen.getByText(/not commandable on the controller/)).toBeInTheDocument();
  });

  it("shows a 409 from create", async () => {
    const user = userEvent.setup();
    vi.mocked(cloneApi.get).mockResolvedValue(done);
    vi.mocked(cloneApi.create).mockRejectedValue(new ApiError("device 4001 already exists", 409));
    renderPanel();
    await screen.findByLabelText("Device instance");
    await user.click(await screen.findByRole("button", { name: "Create device" }));
    expect(await screen.findByText(/device 4001 already exists/)).toBeInTheDocument();
  });

  it("shows the plan's problem and holds Create back", async () => {
    vi.mocked(cloneApi.get).mockResolvedValue(done);
    vi.mocked(cloneApi.plan).mockResolvedValue(planResult({ problem: "device 4001 already exists" }));
    renderPanel();
    expect(await screen.findByText(/device 4001 already exists/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create device" })).toBeDisabled();
  });

  it("offers the request link only when types were skipped", async () => {
    vi.mocked(cloneApi.get).mockResolvedValue(done);
    renderPanel();
    const link = await screen.findByRole("link", { name: "Request support for these types" });
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noreferrer");
    expect(link.getAttribute("href")).not.toContain("kWh");
  });

  it("hides the request link when nothing was skipped for its type", async () => {
    vi.mocked(cloneApi.get).mockResolvedValue(done);
    vi.mocked(cloneApi.plan).mockResolvedValue(
      planResult({
        skipped_types: {},
        skipped: [{ object_type: "analog-input", instance: 4, name: "OAT", reason: "no response", code: "read-failed" }],
      })
    );
    renderPanel();
    expect(await screen.findByText(/no response/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Request support for these types" })).not.toBeInTheDocument();
  });

  it("Discard deletes the job and closes", async () => {
    const user = userEvent.setup();
    vi.mocked(cloneApi.get).mockResolvedValue(done);
    vi.mocked(cloneApi.discard).mockResolvedValue(undefined);
    const onClose = renderPanel();
    await user.click(await screen.findByRole("button", { name: "Discard" }));
    expect(cloneApi.discard).toHaveBeenCalledWith(599001, 7);
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("a failed job shows its error", async () => {
    vi.mocked(cloneApi.get).mockResolvedValue({ ...running, status: "error", error: "no response within 5 s" });
    renderPanel();
    expect(await screen.findByText(/no response within 5 s/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Discard" })).toBeInTheDocument();
  });
});
