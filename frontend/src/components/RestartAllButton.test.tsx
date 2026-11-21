import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import RestartAllButton from "./RestartAllButton";
import { engineApi } from "../api/endpoints";
import type { Notice } from "./Banner";

vi.mock("../api/endpoints", () => ({
  engineApi: { restartAll: vi.fn() },
}));

function renderButton(onNotice: (n: Notice) => void) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <RestartAllButton onNotice={onNotice} />
    </QueryClientProvider>
  );
}

describe("RestartAllButton", () => {
  beforeEach(() => vi.clearAllMocks());

  it("says how many running devices were restarted", async () => {
    vi.mocked(engineApi.restartAll).mockResolvedValue({ restarted: [1, 2], failed: [] });
    const onNotice = vi.fn();
    renderButton(onNotice);

    await userEvent.setup().click(screen.getByRole("button", { name: "Restart all running devices" }));

    expect(engineApi.restartAll).toHaveBeenCalledTimes(1);
    expect(onNotice).toHaveBeenLastCalledWith({ type: "ok", message: "restarted 2 running devices" });
  });

  it("says so when nothing is running", async () => {
    vi.mocked(engineApi.restartAll).mockResolvedValue({ restarted: [], failed: [] });
    const onNotice = vi.fn();
    renderButton(onNotice);

    await userEvent.setup().click(screen.getByRole("button", { name: "Restart all running devices" }));

    expect(onNotice).toHaveBeenLastCalledWith({
      type: "ok",
      message: "No devices are running.",
    });
  });

  it("names each device that failed to come back, with the reason", async () => {
    vi.mocked(engineApi.restartAll).mockResolvedValue({
      restarted: [2],
      failed: [{ device_id: 1, error: "unable to start device 1: Address 192.0.2.1 is not assigned" }],
    });
    const onNotice = vi.fn();
    renderButton(onNotice);

    await userEvent.setup().click(screen.getByRole("button", { name: "Restart all running devices" }));

    expect(onNotice).toHaveBeenLastCalledWith({
      type: "error",
      message:
        "restarted 1 of 2 running devices: unable to start device 1: Address 192.0.2.1 is not assigned",
    });
  });

  it("is disabled while the restart runs", async () => {
    vi.mocked(engineApi.restartAll).mockReturnValue(new Promise(() => {}) as never);
    renderButton(vi.fn());
    const button = screen.getByRole("button", { name: "Restart all running devices" });

    await userEvent.setup().click(button);

    expect(screen.getByRole("button", { name: /restarting/i })).toBeDisabled();
  });
});
