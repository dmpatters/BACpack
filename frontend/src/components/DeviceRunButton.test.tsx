import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import DeviceRunButton from "./DeviceRunButton";
import { engineApi } from "../api/endpoints";
import type { ProcessStatus } from "../api/types";

vi.mock("../api/endpoints", () => ({
  engineApi: { start: vi.fn(), stop: vi.fn(), restart: vi.fn() },
}));

const onError = vi.fn();

function renderButton(status: ProcessStatus | undefined) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <DeviceRunButton deviceId={1} status={status} onError={onError} />
    </QueryClientProvider>
  );
}

function buttonNames(): string[] {
  return screen.getAllByRole("button").map((el) => el.textContent?.trim() ?? "");
}

describe("DeviceRunButton", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(engineApi.restart).mockResolvedValue([]);
  });

  it("offers Restart serving beside Stop on a running device", async () => {
    renderButton("running");
    expect(buttonNames()).toEqual(["Restart serving", "Stop"]);
    await userEvent.setup().click(screen.getByRole("button", { name: "Restart serving" }));
    expect(engineApi.restart).toHaveBeenCalledWith(1);
  });

  it("disables both while the device is reinitializing", () => {
    renderButton("reinitializing");
    expect(screen.getByRole("button", { name: "Restart serving" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Stop" })).toBeDisabled();
  });

  it("offers only Start on a stopped device", () => {
    renderButton("stopped");
    expect(buttonNames()).toEqual(["Start"]);
  });

  it("hands a failed restart to the page and shows nothing beside the buttons", async () => {
    vi.mocked(engineApi.restart).mockRejectedValue(new Error("device 1 is not running"));
    renderButton("running");
    await userEvent.setup().click(screen.getByRole("button", { name: "Restart serving" }));
    await waitFor(() => expect(onError).toHaveBeenCalledWith("device 1 is not running"));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("clears the page's error when an action succeeds", async () => {
    renderButton("running");
    await userEvent.setup().click(screen.getByRole("button", { name: "Restart serving" }));
    await waitFor(() => expect(onError).toHaveBeenCalledWith(null));
  });
});
