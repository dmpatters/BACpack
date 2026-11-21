import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SampleBuildingPanel from "./SampleBuildingPanel";
import { sampleApi } from "../api/endpoints";
import { ApiError } from "../api/http";
import type { SamplePlan } from "../api/types";

vi.mock("../api/endpoints", () => ({
  sampleApi: { plan: vi.fn(), load: vi.fn() },
}));

const plan: SamplePlan = {
  devices: [
    { id: 4190001, name: "Sample AHU-1", role: "bbmd", object_count: 13, bind_address: "192.168.1.50/24:47808", port: 47808 },
    { id: 4190101, name: "Sample VAV-1", role: "normal", object_count: 5, bind_address: "192.168.1.50/24:47809", port: 47809 },
  ],
  refusal: null,
  notes: [
    { text: "A Who-Is on UDP 47808 finds all four devices.", guide: null },
    { text: "Allow UDP 47809-47811.", guide: "sample-building.md#firewall" },
  ],
};

function renderPanel(onClose = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const ui = (open: boolean) => (
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SampleBuildingPanel open={open} onClose={onClose} />
      </MemoryRouter>
    </QueryClientProvider>
  );
  const { rerender } = render(ui(true));
  return Object.assign(onClose, { invalidate, setOpen: (open: boolean) => rerender(ui(open)) });
}

describe("SampleBuildingPanel", () => {
  beforeEach(() => {
    vi.mocked(sampleApi.plan).mockReset();
    vi.mocked(sampleApi.load).mockReset();
  });

  it("shows the plan: each device with its address and port, the notes and a guide link", async () => {
    vi.mocked(sampleApi.plan).mockResolvedValue(plan);
    renderPanel();
    expect(await screen.findByText("Sample AHU-1")).toBeInTheDocument();
    expect(screen.getByText(/192\.168\.1\.50\/24 · UDP 47808/)).toBeInTheDocument();
    expect(screen.getByText(/13 objects/)).toBeInTheDocument();
    expect(screen.getByText(/finds all four devices/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "More" })).toHaveAttribute(
      "href",
      "https://github.com/dmpatters/BACpack/blob/main/docs/guide/sample-building.md#firewall"
    );
    expect(screen.getByRole("checkbox", { name: "Start the devices" })).toBeChecked();
  });

  it("shows a refusal in place of Load, with a link to Network Settings for a missing default bind", async () => {
    vi.mocked(sampleApi.plan).mockResolvedValue({
      ...plan,
      refusal: "Set the server's default bind address on the Network Settings page first.",
    });
    renderPanel();
    expect(await screen.findByText(/Set the server's default bind address/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Network Settings" })).toHaveAttribute("href", "/settings/network");
  });

  it("loads with the box ticked and closes", async () => {
    const user = userEvent.setup();
    vi.mocked(sampleApi.plan).mockResolvedValue(plan);
    vi.mocked(sampleApi.load).mockResolvedValue({ devices: [] });
    const onClose = renderPanel();
    await user.click(await screen.findByRole("button", { name: "Load" }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(sampleApi.load).toHaveBeenCalledWith(true);
    expect(onClose.invalidate).toHaveBeenCalledWith({ queryKey: ["devices"] });
    expect(onClose.invalidate).toHaveBeenCalledWith({ queryKey: ["engineStatus"] });
  });

  it("loads without starting when the box is unticked", async () => {
    const user = userEvent.setup();
    vi.mocked(sampleApi.plan).mockResolvedValue(plan);
    vi.mocked(sampleApi.load).mockResolvedValue({ devices: [] });
    renderPanel();
    await user.click(await screen.findByRole("checkbox", { name: "Start the devices" }));
    await user.click(screen.getByRole("button", { name: "Load" }));
    await waitFor(() => expect(sampleApi.load).toHaveBeenCalledWith(false));
  });

  it("says why a load failed and stays open", async () => {
    const user = userEvent.setup();
    vi.mocked(sampleApi.plan).mockResolvedValue(plan);
    vi.mocked(sampleApi.load).mockRejectedValue(new ApiError("device 4190001 already exists", 409));
    const onClose = renderPanel();
    await user.click(await screen.findByRole("button", { name: "Load" }));
    expect(await screen.findByText(/Couldn't load the sample building/)).toBeInTheDocument();
    expect(screen.getByText(/already exists/)).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("disables the button while loading, saying it is starting the devices when they are ticked", async () => {
    const user = userEvent.setup();
    vi.mocked(sampleApi.plan).mockResolvedValue(plan);
    vi.mocked(sampleApi.load).mockReturnValue(new Promise(() => {}));
    renderPanel();
    await user.click(await screen.findByRole("button", { name: "Load" }));
    expect(screen.getByRole("button", { name: "Loading and starting…" })).toBeDisabled();
  });

  it("says only Load while loading without starting", async () => {
    const user = userEvent.setup();
    vi.mocked(sampleApi.plan).mockResolvedValue(plan);
    vi.mocked(sampleApi.load).mockReturnValue(new Promise(() => {}));
    renderPanel();
    await user.click(await screen.findByRole("checkbox", { name: "Start the devices" }));
    await user.click(screen.getByRole("button", { name: "Load" }));
    expect(screen.getByRole("button", { name: "Load" })).toBeDisabled();
  });

  it("plans again after a failed load, so a refusal replaces Load", async () => {
    const user = userEvent.setup();
    vi.mocked(sampleApi.plan)
      .mockResolvedValueOnce(plan)
      .mockResolvedValue({ ...plan, refusal: "device 4190001 (Sample AHU-1) already exists on this server" });
    vi.mocked(sampleApi.load).mockRejectedValue(new ApiError("device 4190001 already exists", 409));
    renderPanel();
    await user.click(await screen.findByRole("button", { name: "Load" }));
    expect(await screen.findByText(/Can't load it here/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load" })).not.toBeInTheDocument();
  });

  it("does not show an old error when the panel is opened again", async () => {
    const user = userEvent.setup();
    vi.mocked(sampleApi.plan).mockResolvedValue(plan);
    vi.mocked(sampleApi.load).mockRejectedValue(new ApiError("device 4190001 already exists", 409));
    const panel = renderPanel();
    await user.click(await screen.findByRole("button", { name: "Load" }));
    expect(await screen.findByText(/Couldn't load the sample building/)).toBeInTheDocument();
    panel.setOpen(false);
    panel.setOpen(true);
    await waitFor(() => expect(screen.queryByText(/Couldn't load the sample building/)).not.toBeInTheDocument());
  });
});
