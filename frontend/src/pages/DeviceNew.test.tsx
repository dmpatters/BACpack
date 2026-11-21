import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi, beforeEach } from "vitest";
import NewDeviceForm from "./DeviceNew";
import { expectNamedControls } from "../testing/controls";
import { devicesApi } from "../api/endpoints";
import type { DeviceSummary } from "../api/types";

vi.mock("../api/endpoints", () => ({
  devicesApi: { create: vi.fn() },
}));

const onCreated = vi.fn();

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <NewDeviceForm onCreated={onCreated} />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe("NewDeviceForm", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(devicesApi.create).mockResolvedValue({ id: 599100 } as DeviceSummary);
  });

  it("hands the created device to onCreated", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.type(screen.getByLabelText(/instance number/i), "599100");
    await user.type(screen.getByLabelText(/^name/i), "AHU-3");
    await user.click(screen.getByRole("button", { name: /create device/i }));

    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledWith({ id: 599100 }));
  });

  it("creates the device with its location", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.type(screen.getByLabelText(/instance number/i), "599100");
    await user.type(screen.getByLabelText(/^name/i), "AHU-3");
    await user.type(screen.getByLabelText(/location/i), "Roof");
    await user.click(screen.getByRole("button", { name: /create device/i }));

    expect(devicesApi.create).toHaveBeenCalledWith(expect.objectContaining({ id: 599100, location: "Roof" }));
  });

  it("leaves the location out when it is empty", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.type(screen.getByLabelText(/instance number/i), "599100");
    await user.type(screen.getByLabelText(/^name/i), "AHU-3");
    await user.click(screen.getByRole("button", { name: /create device/i }));

    expect(vi.mocked(devicesApi.create).mock.calls[0][0].location).toBeUndefined();
  });

  it("shows a create error under Create device", async () => {
    vi.mocked(devicesApi.create).mockRejectedValue(new Error("instance 599100 is already used"));
    const user = userEvent.setup();
    renderPage();

    await user.type(screen.getByLabelText(/instance number/i), "599100");
    await user.type(screen.getByLabelText(/^name/i), "AHU-3");
    await user.click(screen.getByRole("button", { name: /create device/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Couldn't create the device: instance 599100 is already used");
    expect(screen.getByRole("button", { name: /create device/i }).closest("form")).toContainElement(alert);
  });

  it("names every control", () => {
    const { container } = renderPage();
    expectNamedControls(container);
  });
});
