import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import DeviceTrafficTab from "./DeviceTrafficTab";
import { engineApi, trafficApi } from "../api/endpoints";

vi.mock("../api/endpoints", () => ({
  engineApi: { status: vi.fn() },
  trafficApi: { recent: vi.fn() },
}));

function renderTab() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <DeviceTrafficTab deviceId={1} />
    </QueryClientProvider>
  );
}

describe("DeviceTrafficTab", () => {
  it("says it records the device's own client requests, not just Who-Is and COV", async () => {
    vi.mocked(engineApi.status).mockResolvedValue([]);
    vi.mocked(trafficApi.recent).mockResolvedValue([]);
    renderTab();
    expect(screen.getByText(/requests sent as a client/)).toBeInTheDocument();
    expect(screen.queryByText(/Who-Is\/I-Am and COV activity/)).not.toBeInTheDocument();
  });

  it("is the log table's shape: Time (UTC), a mono event, a detail that may wrap", async () => {
    vi.mocked(engineApi.status).mockResolvedValue([]);
    vi.mocked(trafficApi.recent).mockResolvedValue([
      { timestamp: "2026-10-13T10:00:00Z", event_type: "who-is", detail: "from 192.168.1.50" },
    ]);
    renderTab();
    expect(await screen.findByRole("columnheader", { name: "Time (UTC)" })).toBeInTheDocument();
    expect(screen.getByText("2026-10-13 10:00:00")).toHaveClass("bp-cell-time");
    expect(screen.getByText("who-is")).toHaveClass("bp-cell-mono");
    expect(screen.getByText("from 192.168.1.50")).toHaveClass("bp-cell-msg");
  });
});
