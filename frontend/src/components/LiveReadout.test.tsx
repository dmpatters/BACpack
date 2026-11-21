import { act, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import LiveReadout from "./LiveReadout";
import { liveApi } from "../api/endpoints";
import { ApiError } from "../api/http";
import type { BacnetObjectDTO, ObjectLiveDTO } from "../api/types";

vi.mock("../api/endpoints", () => ({ liveApi: { object: vi.fn() } }));

const LIVE: ObjectLiveDTO = {
  object_id: 5,
  object_type: "analog-value",
  instance: 1,
  present_value: 72.5,
  priority_array: null,
  controlling_priority: null,
  relinquish_default: 70,
  status_flags: [],
  event_state: "normal",
  reliability: "no-fault-detected",
  out_of_service: false,
  read_at: "2026-10-16T10:48:02+00:00",
};

function makeObj(overrides: Partial<BacnetObjectDTO> = {}): BacnetObjectDTO {
  return {
    id: 5,
    device_id: 599001,
    object_type: "analog-value",
    instance_number: 1,
    name: "AV-1",
    state_text: null,
    schedule_value_type: null,
    ...overrides,
  } as BacnetObjectDTO;
}

function renderReadout(obj: BacnetObjectDTO, unitLabel: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <LiveReadout deviceId={599001} obj={obj} unitLabel={unitLabel} />
    </QueryClientProvider>
  );
}

describe("LiveReadout", () => {
  afterEach(() => vi.useRealTimers());

  it("shows the value, flags, event state and the priority array with the controlling slot", async () => {
    vi.mocked(liveApi.object).mockResolvedValue({
      ...LIVE,
      priority_array: [...Array(7).fill(null), 72.5, ...Array(8).fill(null)],
      controlling_priority: 8,
      status_flags: ["overridden"],
    });
    renderReadout(makeObj(), "°F");

    const value = await screen.findByText("72.5", { selector: ".bp-readout__value" });
    expect(within(value.closest(".bp-readout__item") as HTMLElement).getByText("°F")).toBeInTheDocument();
    expect(screen.getByText("overridden")).toHaveClass("bp-pill--warn");
    expect(screen.getByText("normal")).toHaveClass("bp-pill--ok");
    expect(screen.queryByRole("list", { name: /priority array/i })).not.toBeInTheDocument();
    expect(within(value.closest(".bp-readout__item") as HTMLElement).getByText("from priority 8")).toBeInTheDocument();
    expect(screen.getByText(/updated 10:48:02 UTC/)).toBeInTheDocument();
  });

  it("says the value comes from the Relinquish default when no slot is set, with no card of its own", async () => {
    vi.mocked(liveApi.object).mockResolvedValue({ ...LIVE, priority_array: Array(16).fill(null) });
    renderReadout(makeObj(), "°F");
    const value = await screen.findByText("72.5", { selector: ".bp-readout__value" });
    expect(
      within(value.closest(".bp-readout__item") as HTMLElement).getByText("from Relinquish default")
    ).toBeInTheDocument();
    expect(screen.queryByText("Relinquish default")).not.toBeInTheDocument();
  });

  it("says no flags are set in words", async () => {
    vi.mocked(liveApi.object).mockResolvedValue(LIVE);
    renderReadout(makeObj(), "");
    expect(await screen.findByText("none")).toHaveClass("bp-pill");
  });

  it("marks old values stale with their age after a failed poll", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(liveApi.object).mockResolvedValueOnce(LIVE).mockRejectedValue(new ApiError("didn't answer in time", 504));
    renderReadout(makeObj(), "");
    await screen.findByText("72.5");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });

    const stale = await screen.findByText("stale");
    expect(stale).toHaveClass("bp-pill--warn");
    expect(screen.getByText("72.5").closest(".bp-readout__item")).toHaveClass("bp-readout--stale");
    expect(screen.getByText(/ago/)).toBeInTheDocument();
  });

  it("says the device isn't running instead of showing numbers", async () => {
    vi.mocked(liveApi.object).mockRejectedValue(new ApiError("device 599001 isn't running; start it to see live values", 409));
    renderReadout(makeObj(), "");
    expect(await screen.findByText("Device not running: no live values.")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: /priority array/i })).not.toBeInTheDocument();
  });

  it("passes on the older-process message", async () => {
    const message = "device 599001 is running an older BACpack version; restart the device to use live values";
    vi.mocked(liveApi.object).mockRejectedValue(new ApiError(message, 409));
    renderReadout(makeObj(), "");
    expect(await screen.findByText(message)).toBeInTheDocument();
  });

  it("says a just-saved object isn't live yet, not an error", async () => {
    vi.mocked(liveApi.object).mockRejectedValue(new ApiError("object 5 isn't live yet", 404));
    renderReadout(makeObj(), "");
    expect(
      await screen.findByText("Not live yet.")
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("names a multi-state value that isn't one of its states", async () => {
    vi.mocked(liveApi.object).mockResolvedValue({ ...LIVE, object_type: "multi-state-value", present_value: 7 });
    renderReadout(makeObj({ object_type: "multi-state-value", state_text: ["Off", "Low", "High"] }), "");
    expect(await screen.findByText("7 · not a state")).toBeInTheDocument();
  });

  it("leaves out the 'from' line for an object that isn't commandable", async () => {
    vi.mocked(liveApi.object).mockResolvedValue({ ...LIVE, priority_array: null });
    renderReadout(makeObj({ object_type: "analog-input" }), "");
    await screen.findByText("72.5");
    expect(screen.queryByText(/^from /)).not.toBeInTheDocument();
    expect(screen.queryByText("Relinquish default")).not.toBeInTheDocument();
  });
});
