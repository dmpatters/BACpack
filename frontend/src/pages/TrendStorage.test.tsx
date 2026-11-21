import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, RouterProvider, Routes, createMemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { expect, it, vi } from "vitest";
import TrendStorage from "./TrendStorage";
import { LeaveGuard, UnsavedChangesProvider } from "../components/UnsavedChanges";
import { expectNamedControls } from "../testing/controls";
import { settingsApi, trendStorageApi } from "../api/endpoints";
import { inSettingsColumn } from "../testing/columns";

vi.mock("../api/endpoints", () => ({
  settingsApi: { get: vi.fn(), update: vi.fn() },
  trendStorageApi: { get: vi.fn() },
}));

const MB = 1024 * 1024;

it("shows a meter and a table sorted largest first, with no pie chart", async () => {
  vi.mocked(settingsApi.get).mockResolvedValue({ trend_retention_days: null, trend_soft_limit_mb: null } as never);
  vi.mocked(trendStorageApi.get).mockResolvedValue({
    total_samples: 3000,
    total_bytes_estimate: 4 * MB,
    effective_limit_bytes: 1024 * MB,
    hard_cap_bytes: 1024 * MB,
    pruning: false,
    trends: [
      { device_id: 599001, object_id: 1, object_name: "Small", sample_count: 1000, bytes_estimate: 1 * MB, sample_cap: 32767 },
      { device_id: 599002, object_id: 2, object_name: "Big", sample_count: 2000, bytes_estimate: 3 * MB, sample_cap: 4000 },
    ],
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { container } = render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <TrendStorage />
      </MemoryRouter>
    </QueryClientProvider>
  );
  expect(await screen.findByRole("meter", { name: "Trend data" })).toHaveAttribute("aria-valuetext", "4.00 MB of 1024 MB");
  expect(container.querySelector("svg")).toBeNull();
  const rows = within(screen.getByRole("table", { name: "Trend storage by object" })).getAllByRole("row").slice(1);
  expect(rows.map((r) => within(r).getAllByRole("cell")[0].textContent)).toEqual(["Big", "Small"]);
  expect(within(rows[0]).getByRole("link", { name: "Big" })).toHaveAttribute("href", "/devices/599002/objects/2/trending");
  expect(within(rows[0]).getByText("3.00 MB")).toHaveClass("bp-cell-num", "bp-cell-mono");
});

it("fills each trend's bar against its own sample cap, not the share of all trend data", async () => {
  vi.mocked(settingsApi.get).mockResolvedValue({ trend_retention_days: null, trend_soft_limit_mb: null } as never);
  vi.mocked(trendStorageApi.get).mockResolvedValue({
    ...STORAGE,
    total_samples: 34767,
    total_bytes_estimate: 4 * MB,
    trends: [
      { device_id: 599002, object_id: 1, object_name: "Full", sample_count: 32767, bytes_estimate: 3 * MB, sample_cap: 32767 },
      { device_id: 599002, object_id: 2, object_name: "Half", sample_count: 2000, bytes_estimate: 1 * MB, sample_cap: 4000 },
    ],
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <TrendStorage />
      </MemoryRouter>
    </QueryClientProvider>
  );
  const table = await screen.findByRole("table", { name: "Trend storage by object" });
  expect(within(table).getByRole("columnheader", { name: "Fill" })).toBeInTheDocument();
  const full = within(table).getByRole("meter", { name: "Full fill" });
  expect(full).toHaveAttribute("aria-valuenow", "100");
  expect(full).toHaveAttribute("aria-valuetext", "32767 of 32767 samples (100.0%)");
  // A full trend is normal (the oldest samples roll off), so it doesn't warn.
  expect(full.querySelector(".bp-meter__fill")).not.toHaveClass("bp-meter__fill--danger");
  const half = within(table).getByRole("meter", { name: "Half fill" });
  expect(half).toHaveAttribute("aria-valuenow", "50");
  expect(half).toHaveAttribute("aria-valuetext", "2000 of 4000 samples (50.0%)");
});

const STORAGE = {
  total_samples: 0,
  total_bytes_estimate: 0,
  effective_limit_bytes: 1024 * MB,
  hard_cap_bytes: 1024 * MB,
  pruning: false,
  trends: [],
};

function renderStorage() {
  vi.mocked(settingsApi.get).mockResolvedValue({ trend_retention_days: 30, trend_soft_limit_mb: null } as never);
  vi.mocked(trendStorageApi.get).mockResolvedValue(STORAGE);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <TrendStorage />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

it("puts a save error under Save settings", async () => {
  vi.mocked(settingsApi.update).mockRejectedValue(new Error("trend_soft_limit_mb: too large"));
  const user = userEvent.setup();
  renderStorage();
  await user.click(await screen.findByRole("button", { name: "Save settings" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't save: trend_soft_limit_mb: too large");
});

it("names every control", async () => {
  const { container } = renderStorage();
  await screen.findByLabelText("Retention (days)");
  expectNamedControls(container);
});

it("asks before the nav drops an unsaved edit", async () => {
  vi.mocked(settingsApi.get).mockResolvedValue({ trend_retention_days: 30, trend_soft_limit_mb: null } as never);
  vi.mocked(trendStorageApi.get).mockResolvedValue(STORAGE);
  const user = userEvent.setup();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter(
    [
      {
        path: "*",
        element: (
          <UnsavedChangesProvider>
            <Routes>
              <Route path="/trends" element={<TrendStorage />} />
              <Route path="*" element={<p>home</p>} />
            </Routes>
            <LeaveGuard />
          </UnsavedChangesProvider>
        ),
      },
    ],
    { initialEntries: ["/trends"] }
  );
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  );
  await user.type(await screen.findByLabelText("Retention (days)"), "0");
  await act(() => router.navigate("/"));
  expect(screen.getByRole("alertdialog", { name: "Discard unsaved changes?" })).toBeInTheDocument();
});

it('shows "Pruning database" while the history is over its limit', async () => {
  vi.mocked(settingsApi.get).mockResolvedValue({ trend_retention_days: 30, trend_soft_limit_mb: 5 } as never);
  vi.mocked(trendStorageApi.get).mockResolvedValue({ ...STORAGE, pruning: true });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <TrendStorage />
      </MemoryRouter>
    </QueryClientProvider>
  );
  expect(await screen.findByText("Pruning database")).toBeInTheDocument();
  view.unmount();

  vi.mocked(trendStorageApi.get).mockResolvedValue({ ...STORAGE, pruning: false });
  renderStorage();
  await screen.findByRole("meter", { name: "Trend data" });
  expect(screen.queryByText("Pruning database")).toBeNull();
});

it("refetches storage while pruning and stops after", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    vi.mocked(settingsApi.get).mockResolvedValue({ trend_retention_days: 30, trend_soft_limit_mb: 5 } as never);
    vi.mocked(trendStorageApi.get).mockReset();
    vi.mocked(trendStorageApi.get)
      .mockResolvedValueOnce({ ...STORAGE, pruning: true })
      .mockResolvedValue({ ...STORAGE, pruning: false });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <TrendStorage />
        </MemoryRouter>
      </QueryClientProvider>
    );
    expect(await screen.findByText("Pruning database")).toBeInTheDocument();
    expect(trendStorageApi.get).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    await waitFor(() => expect(screen.queryByText("Pruning database")).toBeNull());
    expect(trendStorageApi.get).toHaveBeenCalledTimes(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10000);
    });
    expect(trendStorageApi.get).toHaveBeenCalledTimes(2);
  } finally {
    vi.useRealTimers();
  }
});

it("puts the retention form in the centred settings column, with the meter and table at full width", async () => {
  vi.mocked(settingsApi.get).mockResolvedValue({ trend_retention_days: null, trend_soft_limit_mb: null } as never);
  vi.mocked(trendStorageApi.get).mockResolvedValue({ ...STORAGE, trends: [] });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <TrendStorage />
      </MemoryRouter>
    </QueryClientProvider>
  );
  const retention = await screen.findByRole("group", { name: "Retention" });
  expect(inSettingsColumn(retention)).toBe(true);
  expect(inSettingsColumn(screen.getByRole("meter", { name: "Trend data" }))).toBe(false);
});

it("keeps text typed after Save when the settings refetch lands, and stays dirty", async () => {
  vi.mocked(settingsApi.update).mockResolvedValue({ trend_retention_days: 45, trend_soft_limit_mb: null } as never);
  const user = userEvent.setup();
  renderStorage();
  const retention = await screen.findByLabelText("Retention (days)");
  await user.clear(retention);
  await user.type(retention, "45");

  let land: (value: never) => void = () => undefined;
  vi.mocked(settingsApi.get).mockImplementationOnce(() => new Promise((resolve) => (land = resolve as typeof land)));
  // Counted from here: this file's earlier tests leave calls on the mock.
  const callsBefore = vi.mocked(settingsApi.get).mock.calls.length;
  await user.click(screen.getByRole("button", { name: "Save settings" }));
  // The refetch must be in flight before typing, or `land` is still a no-op
  // and nothing is ever overwritten.
  await waitFor(() => expect(vi.mocked(settingsApi.get).mock.calls.length).toBe(callsBefore + 1));
  await user.type(screen.getByLabelText("Soft limit (MB)"), "7");

  await act(async () => land({ trend_retention_days: 45, trend_soft_limit_mb: null } as never));
  // TanStack hands the refetched data over a tick after act flushes, and the
  // retention field already shows 45 (typed), so wait rather than poll for it.
  await act(() => new Promise((r) => setTimeout(r, 50)));
  expect(screen.getByLabelText("Soft limit (MB)")).toHaveValue(7);
  expect(screen.getByLabelText("Retention (days)")).toHaveValue(45);
  expect(screen.queryByText("Saved.")).not.toBeInTheDocument();
});

it("takes a refetched value for a field the user has not touched, and keeps the edited one", async () => {
  const user = userEvent.setup();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  vi.mocked(settingsApi.get).mockResolvedValue({ trend_retention_days: 30, trend_soft_limit_mb: null } as never);
  vi.mocked(trendStorageApi.get).mockResolvedValue(STORAGE);
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <TrendStorage />
      </MemoryRouter>
    </QueryClientProvider>
  );
  await user.type(await screen.findByLabelText("Soft limit (MB)"), "7");
  vi.mocked(settingsApi.get).mockResolvedValue({ trend_retention_days: 90, trend_soft_limit_mb: null } as never);
  await act(() => client.invalidateQueries({ queryKey: ["networkSettings"] }));
  await waitFor(() => expect(screen.getByLabelText("Retention (days)")).toHaveValue(90));
  expect(screen.getByLabelText("Soft limit (MB)")).toHaveValue(7);
});
