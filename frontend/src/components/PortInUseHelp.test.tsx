import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PortInUseHelp from "./PortInUseHelp";
import { devicesApi, engineApi } from "../api/endpoints";
import { GUIDE_URL } from "../utils/bind";

vi.mock("../api/endpoints", () => ({
  devicesApi: { portSuggestion: vi.fn(), update: vi.fn() },
  engineApi: { start: vi.fn() },
}));

const ERROR = "UDP port 192.168.1.200:47808 is already in use by another program or device";

function renderHelp() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <PortInUseHelp deviceId={599001} error={ERROR} />
    </QueryClientProvider>
  );
}

describe("PortInUseHelp", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(devicesApi.portSuggestion).mockResolvedValue({ bind_address: "192.168.1.200/24:47810", port: 47810 });
    vi.mocked(devicesApi.update).mockResolvedValue({} as never);
    vi.mocked(engineApi.start).mockResolvedValue(undefined as never);
  });

  it("names the taken port, the free one and the firewall", async () => {
    renderHelp();

    expect(
      await screen.findByText(/Port 47808 on 192\.168\.1\.200 is in use, probably by Yabe or Workbench\. Port 47810 is free\./)
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Allow UDP 47810 in the firewall for other computers."
      )
    ).toBeInTheDocument();
  });

  it("links the guide", async () => {
    renderHelp();
    expect(await screen.findByRole("link", { name: "Other ways round this" })).toHaveAttribute("href", GUIDE_URL);
  });

  it("saves the bind address with the new port, then starts the device", async () => {
    const calls: string[] = [];
    vi.mocked(devicesApi.update).mockImplementation(async () => {
      calls.push("update");
      return {} as never;
    });
    vi.mocked(engineApi.start).mockImplementation(async () => {
      calls.push("start");
      return [];
    });
    renderHelp();

    await userEvent.setup().click(await screen.findByRole("button", { name: "Use port 47810" }));

    expect(devicesApi.update).toHaveBeenCalledWith(599001, { bind_address: "192.168.1.200/24:47810" });
    expect(engineApi.start).toHaveBeenCalledWith(599001);
    expect(calls).toEqual(["update", "start"]);
  });

  it("does not start the device when the save fails, and says so", async () => {
    vi.mocked(devicesApi.update).mockRejectedValue(new Error("bind_address: refused"));
    renderHelp();

    await userEvent.setup().click(await screen.findByRole("button", { name: "Use port 47810" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't move to port 47810: bind_address: refused");
    expect(engineApi.start).not.toHaveBeenCalled();
  });

  it("shows only the guide link when no free port was found", async () => {
    vi.mocked(devicesApi.portSuggestion).mockResolvedValue({ bind_address: null, port: null });
    renderHelp();

    expect(await screen.findByRole("link", { name: "Other ways round this" })).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByText(/is free/)).not.toBeInTheDocument();
  });
});
