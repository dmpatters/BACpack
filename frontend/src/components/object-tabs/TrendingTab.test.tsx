import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import TrendingTab from "./TrendingTab";
import { columnOf } from "../../testing/columns";
import { expectNamedControls } from "../../testing/controls";
import { objectsApi, trendApi, trendStorageApi } from "../../api/endpoints";
import type { BacnetObjectDTO, TrendRangeDTO } from "../../api/types";

vi.mock("../../api/endpoints", () => ({
  objectsApi: { update: vi.fn() },
  trendApi: {
    range: vi.fn(),
    exportCsvUrl: vi.fn(() => "/api/devices/599002/objects/5/trend/export.csv"),
  },
  trendStorageApi: { get: vi.fn() },
}));

const EMPTY_RANGE: TrendRangeDTO = {
  mode: "interval",
  interval_seconds: 60,
  step: false,
  samples: [],
  carry_in: null,
  gaps: [],
  thinned: false,
  count_in_range: 0,
  earliest_sample: null,
  start: new Date(Date.now() - 3_600_000).toISOString(),
  end: new Date().toISOString(),
};

function makeObj(overrides: Partial<BacnetObjectDTO> = {}): BacnetObjectDTO {
  return {
    id: 5,
    device_id: 599002,
    object_type: "analog-input",
    instance_number: 1,
    name: "AI-1",
    description: "Simulated analog input",
    units: "degreesFahrenheit",
    bacnet_writable: true,
    out_of_service_default: false,
    relinquish_default: null,
    state_text: null,
    trended: false,
    trend_mode: "interval",
    trend_interval_seconds: 60,
    trend_sample_cap: null,
    ...overrides,
  } as BacnetObjectDTO;
}

function renderTab(obj: BacnetObjectDTO) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <TrendingTab deviceId={599002} obj={obj} />
    </QueryClientProvider>
  );
}

describe("TrendingTab", () => {
  it("offers the trend config fields that used to live in the grid", () => {
    vi.mocked(trendApi.range).mockResolvedValue(EMPTY_RANGE);
    vi.mocked(trendStorageApi.get).mockResolvedValue({
      total_samples: 0,
      total_bytes_estimate: 0,
      effective_limit_bytes: 0,
      hard_cap_bytes: 0,
      pruning: false,
      trends: [],
    });
    renderTab(makeObj({ trended: true, trend_mode: "cov" }));
    expect(screen.getByLabelText(/trended/i)).toBeChecked();
    expect(screen.getByLabelText(/trend mode/i)).toHaveValue("cov");
    expect(screen.getByLabelText(/interval/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/sample cap/i)).toBeInTheDocument();
  });

  it("projects the default cap when the object has no sample cap of its own", async () => {
    vi.mocked(trendApi.range).mockResolvedValue(EMPTY_RANGE);
    vi.mocked(trendStorageApi.get).mockResolvedValue({
      total_samples: 10,
      total_bytes_estimate: 1050,
      effective_limit_bytes: 0,
      hard_cap_bytes: 0,
      pruning: false,
      trends: [{ device_id: 599002, object_id: 5, object_name: "AI-1", sample_count: 10, bytes_estimate: 1050, sample_cap: 32767 }],
    });
    renderTab(makeObj({ trended: true, trend_sample_cap: null }));
    expect(await screen.findByText(/default cap 32767/)).toBeInTheDocument();
    expect(screen.getByLabelText(/sample cap/i)).toHaveAttribute("max", "32767");
  });

  it("saves trend config through objectsApi.update", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(makeObj({ trended: false }));
    await user.click(screen.getByLabelText(/trended/i));
    await user.click(screen.getByRole("button", { name: /save/i }));
    expect(objectsApi.update).toHaveBeenCalledWith(
      599002,
      5,
      expect.objectContaining({ trended: true })
    );
  });

  it("says a running device applies the save", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(makeObj({ trended: false }));

    await user.click(screen.getByRole("button", { name: /save/i }));

    expect(await screen.findByText("Saved.")).toBeInTheDocument();
  });

  it("shows a not-trended banner and no chart when the object isn't trended", () => {
    renderTab(makeObj({ trended: false }));
    expect(screen.getByText(/is not trended/i)).toBeInTheDocument();
    expect(screen.queryByText(/export csv/i)).not.toBeInTheDocument();
  });

  it("renders the chart section once trend samples resolve", async () => {
    vi.mocked(trendApi.range).mockResolvedValue({
      ...EMPTY_RANGE,
      samples: [{ timestamp: new Date(Date.now() - 60_000).toISOString(), value: 42 }],
      count_in_range: 1,
    });
    vi.mocked(trendStorageApi.get).mockResolvedValue({
      total_samples: 1,
      total_bytes_estimate: 105,
      effective_limit_bytes: 1000,
      hard_cap_bytes: 2000,
      pruning: false,
      trends: [{ device_id: 599002, object_id: 5, object_name: "AI-1", sample_count: 1, bytes_estimate: 105, sample_cap: 32767 }],
    });
    renderTab(makeObj({ trended: true }));
    expect(await screen.findByText(/export csv/i)).toBeInTheDocument();
    expect(await screen.findByText(/currently using/i)).toBeInTheDocument();
  });

  it("names the default interval when the object has none of its own", async () => {
    vi.mocked(trendApi.range).mockResolvedValue(EMPTY_RANGE);
    vi.mocked(trendStorageApi.get).mockResolvedValue({
      total_samples: 0,
      total_bytes_estimate: 0,
      effective_limit_bytes: 0,
      hard_cap_bytes: 0,
      pruning: false,
      trends: [],
    });
    renderTab(makeObj({ trended: true, trend_interval_seconds: null }));
    expect(screen.getByText(/Polled every 60s \(default\)/)).toBeInTheDocument();
    expect(screen.queryByText(/null/)).not.toBeInTheDocument();
  });

  const emptyStorage = {
    total_samples: 0,
    total_bytes_estimate: 0,
    effective_limit_bytes: 0,
    hard_cap_bytes: 0,
    pruning: false,
    trends: [],
  };

  it("shows the reporting section only when trended", () => {
    renderTab(makeObj({ trended: false }));
    expect(screen.queryByLabelText(/buffer_ready reporting enabled/i)).not.toBeInTheDocument();
  });

  it("shows reporting fields and the last notified record", () => {
    vi.mocked(trendApi.range).mockResolvedValue(EMPTY_RANGE);
    vi.mocked(trendStorageApi.get).mockResolvedValue(emptyStorage);
    renderTab(
      makeObj({
        trended: true,
        trend_reporting_enabled: true,
        trend_notification_threshold: 3,
        trend_notification_class_instance: 1,
        trend_event_detection_enable: true,
        trend_last_notify_record: 42,
      } as Partial<BacnetObjectDTO>)
    );
    expect(screen.getByLabelText(/buffer_ready reporting enabled/i)).toBeChecked();
    expect(screen.getByLabelText(/notification threshold/i)).toHaveValue(3);
    expect(screen.getByLabelText(/trend log notification class/i)).toHaveValue(1);
    expect(screen.getByText(/last notified at record 42/i)).toBeInTheDocument();
    expect(screen.queryByRole("spinbutton", { name: /last notified/i })).not.toBeInTheDocument();
  });

  it("requires the threshold only while reporting is on", async () => {
    const user = userEvent.setup();
    vi.mocked(trendApi.range).mockResolvedValue(EMPTY_RANGE);
    vi.mocked(trendStorageApi.get).mockResolvedValue(emptyStorage);
    renderTab(makeObj({ trended: true, trend_reporting_enabled: false } as Partial<BacnetObjectDTO>));
    expect(screen.getByLabelText(/notification threshold/i)).not.toBeRequired();
    await user.click(screen.getByLabelText(/buffer_ready reporting enabled/i));
    expect(screen.getByLabelText(/notification threshold/i)).toBeRequired();
  });

  it("saves the reporting fields", async () => {
    const user = userEvent.setup();
    vi.mocked(trendApi.range).mockResolvedValue(EMPTY_RANGE);
    vi.mocked(trendStorageApi.get).mockResolvedValue(emptyStorage);
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(makeObj({ trended: true, trend_notification_threshold: 10, trend_reporting_enabled: false } as Partial<BacnetObjectDTO>));
    await user.click(screen.getByLabelText(/buffer_ready reporting enabled/i));
    await user.type(screen.getByLabelText(/trend log notification class/i), "1");
    await user.click(screen.getByRole("button", { name: /save/i }));
    expect(objectsApi.update).toHaveBeenCalledWith(
      599002,
      5,
      expect.objectContaining({ trend_reporting_enabled: true, trend_notification_class_instance: 1 })
    );
  });

  it("sends only the reporting fields the user changed", async () => {
    const user = userEvent.setup();
    vi.mocked(trendApi.range).mockResolvedValue(EMPTY_RANGE);
    vi.mocked(trendStorageApi.get).mockResolvedValue(emptyStorage);
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(
      makeObj({
        trended: true,
        trend_reporting_enabled: true,
        trend_notification_threshold: 5,
        trend_notification_class_instance: 1,
        trend_event_detection_enable: true,
      } as Partial<BacnetObjectDTO>)
    );
    await user.clear(screen.getByLabelText(/notification threshold/i));
    await user.type(screen.getByLabelText(/notification threshold/i), "7");
    await user.click(screen.getByRole("button", { name: /save/i }));
    const patch = vi.mocked(objectsApi.update).mock.lastCall![2];
    expect(patch).toMatchObject({ trend_notification_threshold: 7 });
    expect(patch).not.toHaveProperty("trend_notification_class_instance");
    expect(patch).not.toHaveProperty("trend_reporting_enabled");
    expect(patch).not.toHaveProperty("trend_event_detection_enable");
  });

  it("offers notify type and event enable for BUFFER_READY reporting", () => {
    vi.mocked(trendApi.range).mockResolvedValue(EMPTY_RANGE);
    vi.mocked(trendStorageApi.get).mockResolvedValue(emptyStorage);
    renderTab(makeObj({ trended: true, trend_notify_type: "event", trend_event_enable: null } as Partial<BacnetObjectDTO>));
    const fieldset = screen.getByRole("group", { name: "BUFFER_READY reporting" });
    expect(within(fieldset).getByLabelText("Notify type")).toHaveValue("event");
    const enable = within(fieldset).getByRole("group", { name: "Event enable" });
    const boxes = within(enable).getAllByRole("checkbox");
    expect(boxes).toHaveLength(3);
    for (const box of boxes) expect(box).toBeChecked();
  });

  it("sends only the notify type when that is all that changed", async () => {
    const user = userEvent.setup();
    vi.mocked(trendApi.range).mockResolvedValue(EMPTY_RANGE);
    vi.mocked(trendStorageApi.get).mockResolvedValue(emptyStorage);
    vi.mocked(objectsApi.update).mockClear();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(makeObj({ trended: true, trend_notify_type: "event", trend_event_enable: null } as Partial<BacnetObjectDTO>));
    await user.selectOptions(screen.getByLabelText("Notify type"), "alarm");
    await user.click(screen.getByRole("button", { name: /save/i }));
    const patch = vi.mocked(objectsApi.update).mock.lastCall![2];
    expect(patch).toEqual({ trend_notify_type: "alarm" });
  });

  it("sends no event enable after a tick off and back on", async () => {
    const user = userEvent.setup();
    vi.mocked(trendApi.range).mockResolvedValue(EMPTY_RANGE);
    vi.mocked(trendStorageApi.get).mockResolvedValue(emptyStorage);
    vi.mocked(objectsApi.update).mockClear();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(makeObj({ trended: true, trend_event_enable: null } as Partial<BacnetObjectDTO>));
    await user.click(screen.getByLabelText("To fault"));
    await user.click(screen.getByLabelText("To fault"));
    await user.click(screen.getByRole("button", { name: /save/i }));
    expect(vi.mocked(objectsApi.update).mock.lastCall![2]).not.toHaveProperty("trend_event_enable");
  });

  it("explains that BUFFER_READY is a to-normal notification", () => {
    vi.mocked(trendApi.range).mockResolvedValue(EMPTY_RANGE);
    vi.mocked(trendStorageApi.get).mockResolvedValue(emptyStorage);
    renderTab(makeObj({ trended: true } as Partial<BacnetObjectDTO>));
    expect(
      screen.getByText("BUFFER_READY uses To normal only.")
    ).toBeInTheDocument();
  });

  it("sends the event enable list when a transition is unticked", async () => {
    const user = userEvent.setup();
    vi.mocked(trendApi.range).mockResolvedValue(EMPTY_RANGE);
    vi.mocked(trendStorageApi.get).mockResolvedValue(emptyStorage);
    vi.mocked(objectsApi.update).mockClear();
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(makeObj({ trended: true, trend_notify_type: "event", trend_event_enable: null } as Partial<BacnetObjectDTO>));
    await user.click(screen.getByLabelText("To fault"));
    await user.click(screen.getByRole("button", { name: /save/i }));
    const patch = vi.mocked(objectsApi.update).mock.lastCall![2];
    expect(patch).toEqual({ trend_event_enable: [true, false, true] });
  });

  it("turns reporting off when Trended is unchecked", async () => {
    const user = userEvent.setup();
    vi.mocked(trendApi.range).mockResolvedValue(EMPTY_RANGE);
    vi.mocked(trendStorageApi.get).mockResolvedValue(emptyStorage);
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(makeObj({ trended: true, trend_reporting_enabled: true } as Partial<BacnetObjectDTO>));
    await user.click(screen.getByLabelText(/^trended$/i));
    await user.click(screen.getByRole("button", { name: /save/i }));
    expect(vi.mocked(objectsApi.update).mock.lastCall![2]).toMatchObject({
      trended: false,
      trend_reporting_enabled: false,
    });
  });

  it("puts a save error under Save", async () => {
    vi.mocked(objectsApi.update).mockRejectedValue(new Error("trend_sample_cap: too large"));
    const user = userEvent.setup();
    renderTab(makeObj({ trended: false }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    const form = screen.getByRole("button", { name: "Save" }).closest("form")!;
    expect(await within(form).findByRole("alert")).toHaveTextContent("Couldn't save: trend_sample_cap: too large");
  });

  it("names every control, BUFFER_READY's included", () => {
    const { container } = renderTab(makeObj({ trended: true }));
    expectNamedControls(container.querySelector("form")!);
  });

  it("keeps an unsaved edit when another tab's save refreshes the object", async () => {
    const user = userEvent.setup();
    const client = new QueryClient();
    const tab = (o: BacnetObjectDTO) => (
      <QueryClientProvider client={client}>
        <TrendingTab deviceId={599002} obj={o} />
      </QueryClientProvider>
    );
    const { rerender } = render(tab(makeObj({ trended: false, trend_mode: "interval" })));
    await user.type(screen.getByLabelText("Sample cap"), "100");
    rerender(tab(makeObj({ trended: false, trend_mode: "cov" })));
    expect(screen.getByLabelText("Trend mode")).toHaveValue("cov");
    expect(screen.getByLabelText("Sample cap")).toHaveValue(100);
  });

  it("saves a change back to the old value before the object refetches", async () => {
    vi.mocked(trendApi.range).mockResolvedValue(EMPTY_RANGE);
    vi.mocked(objectsApi.update).mockReset().mockResolvedValue(makeObj());
    const user = userEvent.setup();
    renderTab(makeObj({ trended: false }));
    await user.click(screen.getByLabelText(/trended/i));
    await user.click(screen.getByRole("button", { name: /save/i }));
    await screen.findByText("Saved.");
    await user.click(screen.getByLabelText(/trended/i));
    await user.click(screen.getByRole("button", { name: /save/i }));
    await waitFor(() => expect(objectsApi.update).toHaveBeenCalledTimes(2));
    expect(vi.mocked(objectsApi.update).mock.calls[1][2]).toMatchObject({ trended: false });
  });
});

describe("TrendingTab chart", () => {
  const storage = { total_samples: 0, total_bytes_estimate: 0, effective_limit_bytes: 0, hard_cap_bytes: 0, pruning: false, trends: [] };
  const sampleNow = (value: number) => ({
    ...EMPTY_RANGE,
    samples: [{ timestamp: new Date(Date.now() - 60_000).toISOString(), value }],
    count_in_range: 1,
  });
  // The server's last hour, with a sample at its own now.
  const serverHour = (value: number, serverNowMs: number): TrendRangeDTO => ({
    ...EMPTY_RANGE,
    samples: [{ timestamp: new Date(serverNowMs).toISOString(), value }],
    count_in_range: 1,
    start: new Date(serverNowMs - 3_600_000).toISOString(),
    end: new Date(serverNowMs).toISOString(),
  });
  const lastCall = () => vi.mocked(trendApi.range).mock.calls.at(-1)!;

  it("starts at the last hour, in UTC, with points for the plot's width", async () => {
    vi.mocked(trendStorageApi.get).mockResolvedValue(storage);
    vi.mocked(trendApi.range).mockResolvedValue(sampleNow(42));
    renderTab(makeObj({ trended: true }));
    const picker = screen.getByRole("radiogroup", { name: "Range" });
    expect(within(picker).getByRole("radio", { name: "1 h" })).toBeChecked();
    expect(within(picker).getAllByRole("radio").map((r) => r.closest("label")?.textContent)).toEqual([
      "1 h", "6 h", "24 h", "7 d", "30 d", "Custom",
    ]);
    expect(screen.getByText("Times in UTC")).toBeInTheDocument();
    expect(await screen.findByRole("img", { name: /AI-1/ })).toBeInTheDocument();
    const [deviceId, objectId, params] = lastCall();
    expect([deviceId, objectId, params]).toEqual([599002, 5, { last: 3600, maxPoints: 280 }]);
  });

  it.each([
    ["2 h ahead", 2 * 3_600_000],
    ["3 min behind", -3 * 60_000],
  ])("shows the newest sample with the browser's clock %s", async (_name, skewMs) => {
    const serverNow = Date.now();
    const browserNow = vi.spyOn(Date, "now").mockReturnValue(serverNow + skewMs);
    try {
      vi.mocked(trendStorageApi.get).mockResolvedValue(storage);
      vi.mocked(trendApi.range).mockResolvedValue(serverHour(42, serverNow));
      renderTab(makeObj({ trended: true }));
      expect(await screen.findByRole("img", { name: /latest 42/ })).toBeInTheDocument();
    } finally {
      browserNow.mockRestore();
    }
  });

  it("echoes a custom range's start in ISO form, in UTC", async () => {
    const user = userEvent.setup();
    vi.mocked(trendStorageApi.get).mockResolvedValue(storage);
    vi.mocked(trendApi.range).mockResolvedValue(sampleNow(42));
    renderTab(makeObj({ trended: true }));
    await user.click(within(screen.getByRole("radiogroup", { name: "Range" })).getByRole("radio", { name: "Custom" }));
    const start = screen.getByLabelText("Start (UTC)");
    fireEvent.change(start, { target: { value: "2026-10-16T08:05" } });
    expect(within(start.closest(".bp-field") as HTMLElement).getByTestId("iso-echo")).toHaveTextContent(
      "2026-10-16 08:05 UTC"
    );
  });

  it("keeps the previous chart, dimmed, while a new range loads", async () => {
    const user = userEvent.setup();
    vi.mocked(trendStorageApi.get).mockResolvedValue(storage);
    vi.mocked(trendApi.range).mockResolvedValueOnce(sampleNow(42)).mockReturnValueOnce(new Promise(() => {}));
    const { container } = renderTab(makeObj({ trended: true }));
    await screen.findByRole("img", { name: /AI-1/ });
    await user.click(screen.getByRole("radio", { name: "24 h" }));
    expect(lastCall()[2]).toEqual({ last: 86400, maxPoints: 280 });
    expect(container.querySelector("section.bp-chart")).toHaveClass("bp-chart--stale");
  });

  it("refreshes a range that ends now every 5 s", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      vi.mocked(trendStorageApi.get).mockResolvedValue(storage);
      vi.mocked(trendApi.range).mockResolvedValue(sampleNow(42));
      renderTab(makeObj({ trended: true }));
      await screen.findByRole("img", { name: /AI-1/ });
      const before = vi.mocked(trendApi.range).mock.calls.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5000);
      });
      expect(vi.mocked(trendApi.range).mock.calls.length).toBeGreaterThan(before);
    } finally {
      vi.useRealTimers();
    }
  });

  it("asks for a custom range in UTC and doesn't refresh it", async () => {
    const user = userEvent.setup();
    vi.mocked(trendStorageApi.get).mockResolvedValue(storage);
    vi.mocked(trendApi.range).mockResolvedValue(sampleNow(42));
    renderTab(makeObj({ trended: true }));
    await screen.findByRole("img", { name: /AI-1/ });
    await user.click(screen.getByRole("radio", { name: "Custom" }));
    fireEvent.change(screen.getByLabelText("Start (UTC)"), { target: { value: "2027-01-15T08:00" } });
    fireEvent.change(screen.getByLabelText("End (UTC)"), { target: { value: "2027-01-15T09:00" } });
    await waitFor(() => expect(Date.parse(lastCall()[2].start!)).toBe(Date.parse("2027-01-15T08:00:00Z")));
    expect(Date.parse(lastCall()[2].end!)).toBe(Date.parse("2027-01-15T09:00:00Z"));
    expect(lastCall()[2].last).toBeUndefined();
  });

  it("refuses a custom range whose end is before its start, and sends nothing", async () => {
    const user = userEvent.setup();
    vi.mocked(trendStorageApi.get).mockResolvedValue(storage);
    vi.mocked(trendApi.range).mockResolvedValue(sampleNow(42));
    renderTab(makeObj({ trended: true }));
    await screen.findByRole("img", { name: /AI-1/ });
    await user.click(screen.getByRole("radio", { name: "Custom" }));
    const before = vi.mocked(trendApi.range).mock.calls.length;
    fireEvent.change(screen.getByLabelText("Start (UTC)"), { target: { value: "2027-01-15T09:00" } });
    fireEvent.change(screen.getByLabelText("End (UTC)"), { target: { value: "2027-01-15T08:00" } });
    expect(await screen.findByText("End is before start.")).toBeInTheDocument();
    expect(screen.getByLabelText("End (UTC)")).toHaveAttribute("aria-invalid", "true");
    expect(vi.mocked(trendApi.range).mock.calls.length).toBe(before);
  });

  it("keeps the previous chart when a load fails, and says so", async () => {
    const user = userEvent.setup();
    vi.mocked(trendStorageApi.get).mockResolvedValue(storage);
    vi.mocked(trendApi.range).mockResolvedValueOnce(sampleNow(42)).mockRejectedValueOnce(new Error("boom"));
    renderTab(makeObj({ trended: true }));
    await screen.findByRole("img", { name: /AI-1/ });
    await user.click(screen.getByRole("radio", { name: "6 h" }));
    expect(await screen.findByText(/Couldn't load the trend/)).toBeInTheDocument();
    expect(screen.getByText(/previous result/)).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /AI-1/ })).toBeInTheDocument();
  });

  it.each([
    [{ object_type: "multi-state-value", state_text: ["Off", "Low", "High"] }, ["Off", "Low", "High"]],
    [{ object_type: "binary-value" }, ["Inactive", "Active"]],
    [{ object_type: "calendar" }, ["Not in calendar", "In calendar"]],
  ])("names a %o object's values on the axis", async (overrides, names) => {
    vi.mocked(trendStorageApi.get).mockResolvedValue(storage);
    vi.mocked(trendApi.range).mockResolvedValue({ ...sampleNow(1), step: true });
    const { container } = renderTab(makeObj({ trended: true, ...overrides } as Partial<BacnetObjectDTO>));
    await screen.findByRole("img", { name: /AI-1/ });
    const labels = [...container.querySelectorAll("text.bp-chart__text")].map((t) => t.textContent);
    expect(labels).toEqual(expect.arrayContaining(names));
  });

  it("doesn't call a multi-state schedule's values 'not a state' when it has no state names", async () => {
    vi.mocked(trendStorageApi.get).mockResolvedValue(storage);
    vi.mocked(trendApi.range).mockResolvedValue({ ...sampleNow(2), step: true });
    const { container } = renderTab(
      makeObj({ trended: true, object_type: "schedule", schedule_value_type: "multi-state", state_text: null } as Partial<BacnetObjectDTO>)
    );
    await screen.findByRole("img", { name: /AI-1/ });
    const labels = [...container.querySelectorAll("text.bp-chart__text")].map((t) => t.textContent ?? "");
    expect(labels.some((l) => l.includes("not a state"))).toBe(false);
  });

  it("drops the old refresh button and sample count line", async () => {
    vi.mocked(trendStorageApi.get).mockResolvedValue(storage);
    vi.mocked(trendApi.range).mockResolvedValue(sampleNow(42));
    renderTab(makeObj({ trended: true }));
    await screen.findByRole("img", { name: /AI-1/ });
    expect(screen.queryByRole("button", { name: "Refresh" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Showing the most recent/)).not.toBeInTheDocument();
    expect(screen.getByText(/export csv/i)).toBeInTheDocument();
  });

  it("asks for no range while the object isn't trended", () => {
    const before = vi.mocked(trendApi.range).mock.calls.length;
    renderTab(makeObj({ trended: false }));
    expect(screen.queryByRole("radiogroup", { name: "Range" })).not.toBeInTheDocument();
    expect(vi.mocked(trendApi.range).mock.calls.length).toBe(before);
  });
});

describe("TrendingTab on a wide screen", () => {
  it("puts the settings and the range control in different columns", () => {
    vi.mocked(trendApi.range).mockResolvedValue(EMPTY_RANGE);
    vi.mocked(trendStorageApi.get).mockResolvedValue({
      total_samples: 0,
      total_bytes_estimate: 0,
      effective_limit_bytes: 0,
      hard_cap_bytes: 0,
      pruning: false,
      trends: [],
    });
    renderTab(makeObj({ trended: true }));
    expect(columnOf(screen.getByRole("checkbox", { name: "Trended" }))).toBe(0);
    expect(columnOf(screen.getByRole("radiogroup", { name: "Range" }))).toBe(1);
    expect(columnOf(screen.getByRole("link", { name: "Export CSV" }))).toBe(1);
  });

  it("shows the Trend Log instance and name, placeholders naming the defaults", () => {
    vi.mocked(trendApi.range).mockResolvedValue(EMPTY_RANGE);
    renderTab(makeObj({ trended: true, trend_log_instance: null, trend_log_name: "Zone Temp Log" }));
    expect(screen.getByLabelText("Trend Log instance")).toHaveValue(null);
    expect(screen.getByLabelText("Trend Log instance")).toHaveAttribute("placeholder", "5");
    expect(screen.getByLabelText("Trend Log name")).toHaveValue("Zone Temp Log");
    expect(screen.getByLabelText("Trend Log name")).toHaveAttribute("placeholder", "AI-1-Trend");
  });

  it("saves a fixed Trend Log instance and name", async () => {
    const user = userEvent.setup();
    vi.mocked(trendApi.range).mockResolvedValue(EMPTY_RANGE);
    vi.mocked(objectsApi.update).mockResolvedValue(makeObj());
    renderTab(makeObj({ trended: true }));
    await user.type(screen.getByLabelText("Trend Log instance"), "77");
    await user.type(screen.getByLabelText("Trend Log name"), "Zone Temp Log");
    await user.click(screen.getByRole("button", { name: /save/i }));
    expect(vi.mocked(objectsApi.update).mock.lastCall![2]).toEqual({
      trend_log_instance: 77,
      trend_log_name: "Zone Temp Log",
    });
  });

  it("an out-of-range Trend Log instance blocks the save", async () => {
    const user = userEvent.setup();
    vi.mocked(trendApi.range).mockResolvedValue(EMPTY_RANGE);
    vi.mocked(objectsApi.update).mockClear();
    renderTab(makeObj({ trended: true }));
    await user.type(screen.getByLabelText("Trend Log instance"), "4194303");
    await user.click(screen.getByRole("button", { name: /save/i }));
    expect(await screen.findByText(/0 to 4194302/)).toBeInTheDocument();
    expect(objectsApi.update).not.toHaveBeenCalled();
  });
});
