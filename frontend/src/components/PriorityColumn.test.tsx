import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PriorityColumn from "./PriorityColumn";
import { liveApi } from "../api/endpoints";
import { ApiError } from "../api/http";
import type { BacnetObjectDTO, ObjectLiveDTO } from "../api/types";

vi.mock("../api/endpoints", () => ({ liveApi: { object: vi.fn() } }));

const LIVE: ObjectLiveDTO = {
  object_id: 5,
  object_type: "analog-output",
  instance: 1,
  present_value: 50,
  priority_array: [...Array(7).fill(null), 50, ...Array(7).fill(null), 20],
  controlling_priority: 8,
  relinquish_default: 70,
  status_flags: [],
  event_state: "normal",
  reliability: "no-fault-detected",
  out_of_service: false,
  read_at: "2026-10-16T10:48:02+00:00",
};

const OBJ = {
  id: 5,
  device_id: 599001,
  object_type: "analog-output",
  instance_number: 1,
  name: "AO-1",
  state_text: null,
  schedule_value_type: null,
} as BacnetObjectDTO;

function renderColumn() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <PriorityColumn deviceId={599001} obj={OBJ} />
    </QueryClientProvider>
  );
}

describe("PriorityColumn", () => {
  beforeEach(() => vi.clearAllMocks());

  it("lists the 16 slots with names, the controlling slot, an outranked slot and the relinquish default", async () => {
    vi.mocked(liveApi.object).mockResolvedValue(LIVE);
    renderColumn();
    const list = await screen.findByRole("list", { name: /priority slots/i });
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(16);
    expect(rows[0]).toHaveTextContent("1 Manual life safety —");
    expect(rows[7]).toHaveTextContent("8 Manual operator 50");
    expect(rows[7]).toHaveAttribute("aria-current", "true");
    expect(rows[7]).not.toHaveAttribute("data-outranked");
    expect(rows[15]).toHaveTextContent("16 20");
    expect(rows[15]).toHaveAttribute("data-outranked");
    expect(rows[0]).not.toHaveAttribute("data-outranked");
    expect(screen.getByText("Relinquish default").closest("div")).toHaveTextContent("Relinquish default 70");
  });

  it("names the column once, and gives the Relinquish default its unit", async () => {
    vi.mocked(liveApi.object).mockResolvedValue(LIVE);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <PriorityColumn deviceId={599001} obj={{ ...OBJ, units: "degreesFahrenheit" } as BacnetObjectDTO} />
      </QueryClientProvider>
    );
    await screen.findByRole("list", { name: "Priority slots" });
    expect(screen.getAllByLabelText("Priority array")).toHaveLength(1);
    expect(screen.getByText("Relinquish default").closest("div")).toHaveTextContent("Relinquish default 70 °F");
  });

  it("renders nothing for a reply with no priority array", async () => {
    vi.mocked(liveApi.object).mockResolvedValue({ ...LIVE, priority_array: null });
    renderColumn();
    await vi.waitFor(() => expect(liveApi.object).toHaveBeenCalled());
    expect(screen.queryByRole("list", { name: /priority slots/i })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Priority array")).not.toBeInTheDocument();
  });

  it("says the device isn't running instead of showing slots", async () => {
    vi.mocked(liveApi.object).mockRejectedValue(new ApiError("device 599001 isn't running", 409));
    renderColumn();
    expect(await screen.findByText("Device not running: no live values.")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: /priority slots/i })).not.toBeInTheDocument();
  });

  it("dims the column when the values are stale", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(liveApi.object).mockResolvedValueOnce(LIVE).mockRejectedValue(new ApiError("didn't answer in time", 504));
    renderColumn();
    const column = await screen.findByLabelText("Priority array", { selector: "aside" });
    expect(column).not.toHaveClass("bp-prio-col--stale");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    await vi.waitFor(() => expect(column).toHaveClass("bp-prio-col--stale"));
    vi.useRealTimers();
  });

  it("toggles the names on a narrow screen", async () => {
    const user = userEvent.setup();
    vi.mocked(liveApi.object).mockResolvedValue(LIVE);
    renderColumn();
    const toggle = await screen.findByRole("button", { name: "Show priority names" });
    const column = screen.getByLabelText("Priority array", { selector: "aside" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(column).not.toHaveAttribute("data-expanded");
    await user.click(toggle);
    expect(screen.getByRole("button", { name: "Hide priority names" })).toHaveAttribute("aria-expanded", "true");
    expect(column).toHaveAttribute("data-expanded");
  });
});
