import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import DeviceClock from "./DeviceClock";
import { devicesApi } from "../api/endpoints";
import type { DeviceClockState } from "../api/types";

vi.mock("../api/endpoints", () => ({ devicesApi: { setClock: vi.fn() } }));

function renderClock(clock: DeviceClockState) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <DeviceClock deviceId={1} clock={clock} />
    </QueryClientProvider>
  );
}

const ahead: DeviceClockState = {
  clock_offset_seconds: 192,
  clock_change_pending: false,
  device_time: "2026-09-25T14:03:12-04:00",
};

describe("DeviceClock", () => {
  beforeEach(() => {
    vi.mocked(devicesApi.setClock).mockReset();
    vi.mocked(devicesApi.setClock).mockResolvedValue(ahead);
  });

  it("shows the device wall time and the offset", () => {
    renderClock(ahead);
    expect(screen.getByTestId("device-clock-reading")).toHaveTextContent(
      "2026-09-25 14:03:12 device time · +3m 12s ahead of host"
    );
  });

  it("marks a pending change", () => {
    renderClock({ ...ahead, clock_change_pending: true });
    expect(screen.getByTestId("device-clock-reading")).toHaveTextContent("· applying…");
  });

  it("disables reset when already in sync", () => {
    renderClock({ ...ahead, clock_offset_seconds: 0.3 });
    expect(screen.getByRole("button", { name: "Reset to host time" })).toBeDisabled();
  });

  it("resets with offset_seconds 0", async () => {
    const user = userEvent.setup();
    renderClock(ahead);
    await user.click(screen.getByRole("button", { name: "Reset to host time" }));
    expect(devicesApi.setClock).toHaveBeenCalledWith(1, { offset_seconds: 0 });
  });

  it("echoes the new device time in ISO form, in device time", () => {
    renderClock(ahead);
    const input = screen.getByLabelText("New device time");
    fireEvent.change(input, { target: { value: "2026-10-16T08:05" } });
    expect(within(input.closest(".bp-field") as HTMLElement).getByTestId("iso-echo")).toHaveTextContent(
      "2026-10-16 08:05 device time"
    );
  });

  it("sets the device time from the input", async () => {
    const user = userEvent.setup();
    renderClock(ahead);
    await user.click(screen.getByRole("button", { name: "Set device clock" }));
    expect(devicesApi.setClock).toHaveBeenCalledWith(1, { device_time: "2026-09-25T14:03:12" });
  });

  it("reports the result under its buttons", async () => {
    const user = userEvent.setup();
    renderClock(ahead);
    await user.click(screen.getByRole("button", { name: "Reset to host time" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Device clock reset to host time.");
    vi.mocked(devicesApi.setClock).mockRejectedValue(new Error("device not running"));
    await user.click(screen.getByRole("button", { name: "Set device clock" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't set the device clock: device not running");
  });
});
