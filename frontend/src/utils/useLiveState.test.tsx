import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/http";
import { liveApi } from "../api/endpoints";
import { ageText, classifyLiveError, useDeviceLives, useObjectLive } from "./useLiveState";

vi.mock("../api/endpoints", () => ({ liveApi: { object: vi.fn(), device: vi.fn() } }));

const LIVE = {
  object_id: 5,
  object_type: "analog-value",
  instance: 1,
  present_value: 72.5,
  priority_array: null,
  controlling_priority: null,
  relinquish_default: null,
  status_flags: [],
  event_state: "normal",
  reliability: null,
  out_of_service: false,
  read_at: "2026-10-16T10:48:02+00:00",
};

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("classifyLiveError", () => {
  it("tells the API's answers apart", () => {
    expect(classifyLiveError(new ApiError("device 1 isn't running; start it to see live values", 409))).toBe("stopped");
    expect(classifyLiveError(new ApiError("...; restart the device to use live values", 409))).toBe("outdated");
    expect(classifyLiveError(new ApiError("object 5 isn't live yet", 404))).toBe("missing");
    expect(classifyLiveError(new ApiError("device 1 didn't answer in time", 504))).toBe("stale");
    expect(classifyLiveError(new TypeError("Failed to fetch"))).toBe("stale");
  });
});

describe("ageText", () => {
  it("rounds to the largest unit", () => {
    const at = Date.parse("2026-10-16T10:48:02Z");
    expect(ageText("2026-10-16T10:48:02+00:00", at + 12_000)).toBe("12 s ago");
    expect(ageText("2026-10-16T10:48:02+00:00", at + 4 * 60_000)).toBe("4 min ago");
    expect(ageText("2026-10-16T10:48:02+00:00", at + 2 * 3_600_000)).toBe("2 h ago");
  });
});

describe("useObjectLive", () => {
  beforeEach(() => {
    vi.mocked(liveApi.object).mockReset();
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });
  afterEach(() => vi.useRealTimers());

  it("keeps the last values as stale when a poll fails", async () => {
    vi.mocked(liveApi.object).mockResolvedValueOnce(LIVE).mockRejectedValueOnce(new ApiError("didn't answer in time", 504));
    const { result } = renderHook(() => useObjectLive(599001, 5), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("live"));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });

    await waitFor(() => expect(result.current.status).toBe("stale"));
    expect(result.current.data?.present_value).toBe(72.5);
  });

  it("keeps polling while the device is stopped and comes back live", async () => {
    vi.mocked(liveApi.object)
      .mockRejectedValueOnce(new ApiError("device 599001 isn't running; start it to see live values", 409))
      .mockResolvedValue(LIVE);
    const { result } = renderHook(() => useObjectLive(599001, 5), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("stopped"));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });

    await waitFor(() => expect(result.current.status).toBe("live"));
  });

  it("polls an older device process slowly, and comes back live once it is restarted", async () => {
    vi.mocked(liveApi.object)
      .mockRejectedValueOnce(
        new ApiError("device 599001 is running an older BACpack version; restart the device to use live values", 409)
      )
      .mockResolvedValue(LIVE);
    const { result } = renderHook(() => useObjectLive(599001, 5), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("outdated"));
    expect(result.current.message).toMatch(/restart the device/);
    const calls = vi.mocked(liveApi.object).mock.calls.length;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(15000);
    });
    expect(vi.mocked(liveApi.object).mock.calls.length).toBe(calls);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(16000);
    });
    await waitFor(() => expect(result.current.status).toBe("live"));
  });
});

describe("useDeviceLives", () => {
  it("merges each device's objects by object id and skips a failed device", async () => {
    vi.mocked(liveApi.device).mockImplementation(async (id: number) => {
      if (id === 2) throw new ApiError("isn't running", 409);
      return { read_at: LIVE.read_at, objects: [{ ...LIVE, object_id: 7 }] };
    });
    const { result } = renderHook(() => useDeviceLives([1, 2]), { wrapper });
    await waitFor(() => expect(result.current.get(7)?.present_value).toBe(72.5));
    expect(result.current.size).toBe(1);
  });

  it("drops a device's values once a poll fails, so an old value never looks current", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      vi.mocked(liveApi.device)
        .mockResolvedValueOnce({ read_at: LIVE.read_at, objects: [{ ...LIVE, object_id: 7 }] })
        .mockRejectedValue(new ApiError("device 1 didn't answer in time", 504));
      const { result } = renderHook(() => useDeviceLives([1]), { wrapper });
      await waitFor(() => expect(result.current.get(7)).toBeDefined());

      await act(async () => {
        await vi.advanceTimersByTimeAsync(5000);
      });

      await waitFor(() => expect(result.current.get(7)).toBeUndefined());
    } finally {
      vi.useRealTimers();
    }
  });

  it("polls an older device process slowly", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      vi.mocked(liveApi.device).mockImplementation(async () => {
        throw new ApiError("device 3 is running an older BACpack version; restart the device to use live values", 409);
      });
      renderHook(() => useDeviceLives([3]), { wrapper });
      await waitFor(() => expect(vi.mocked(liveApi.device)).toHaveBeenCalledWith(3));
      const calls = vi.mocked(liveApi.device).mock.calls.length;

      await act(async () => {
        await vi.advanceTimersByTimeAsync(15000);
      });

      expect(vi.mocked(liveApi.device).mock.calls.length).toBe(calls);
    } finally {
      vi.useRealTimers();
    }
  });
});
