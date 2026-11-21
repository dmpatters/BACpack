import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import RemotePointPicker from "./RemotePointPicker";
import { clientToolApi } from "../api/endpoints";

vi.mock("../api/endpoints", () => ({ clientToolApi: { objectList: vi.fn() } }));

describe("RemotePointPicker", () => {
  beforeEach(() => vi.clearAllMocks());

  it("browses the remote device and reports the picked object", async () => {
    vi.mocked(clientToolApi.objectList).mockResolvedValue([
      { object_type: "device", instance: 1234, name: "Remote" },
      { object_type: "analog-input", instance: 1, name: "Zone temp" },
    ]);
    const onPick = vi.fn();
    const user = userEvent.setup();
    render(<RemotePointPicker deviceId={599001} target={{ instance: 1234, address: null }} onPick={onPick} />);

    await user.click(screen.getByRole("button", { name: /browse remote device/i }));
    await user.selectOptions(await screen.findByLabelText("Remote object"), "analog-input:1 (Zone temp)");

    expect(clientToolApi.objectList).toHaveBeenCalledWith(599001, { instance: 1234, address: null });
    expect(onPick).toHaveBeenCalledWith({ object_type: "analog-input", instance: 1, name: "Zone temp" });
  });

  it("offers only the objects `offer` accepts", async () => {
    vi.mocked(clientToolApi.objectList).mockResolvedValue([
      { object_type: "device", instance: 1234, name: "Remote" },
      { object_type: "analog-value", instance: 9, name: "Setpoint" },
    ]);
    const user = userEvent.setup();
    render(
      <RemotePointPicker
        deviceId={599001}
        target={{ instance: 1234 }}
        onPick={() => {}}
        offer={(o) => o.object_type !== "device"}
      />
    );

    await user.click(screen.getByRole("button", { name: /browse remote device/i }));

    const select = await screen.findByLabelText("Remote object");
    expect(select).toHaveTextContent("pick one of 1 objects");
    expect(select).toHaveTextContent("analog-value:9 (Setpoint)");
    expect(select).not.toHaveTextContent("device:1234");
  });

  it("won't browse without a target", () => {
    render(<RemotePointPicker deviceId={599001} target={null} onPick={() => {}} />);

    expect(screen.getByRole("button", { name: /browse remote device/i })).toBeDisabled();
    expect(screen.getByText(/enter a device instance first/)).toBeInTheDocument();
    expect(clientToolApi.objectList).not.toHaveBeenCalled();
  });

  it("shows why browsing failed", async () => {
    vi.mocked(clientToolApi.objectList).mockRejectedValue(new Error("device 599001 isn't running"));
    const user = userEvent.setup();
    render(<RemotePointPicker deviceId={599001} target={{ instance: 1234 }} onPick={() => {}} />);

    await user.click(screen.getByRole("button", { name: /browse remote device/i }));

    expect(await screen.findByText(/isn't running/)).toBeInTheDocument();
  });

  it("drops the browsed list and any error when the target device changes", async () => {
    vi.mocked(clientToolApi.objectList).mockResolvedValue([{ object_type: "analog-value", instance: 9, name: "Setpoint" }]);
    const user = userEvent.setup();
    const { rerender } = render(<RemotePointPicker deviceId={599001} target={{ instance: 1234 }} onPick={() => {}} />);
    await user.click(screen.getByRole("button", { name: /browse remote device/i }));
    expect(await screen.findByLabelText("Remote object")).toBeInTheDocument();

    rerender(<RemotePointPicker deviceId={599001} target={{ instance: 5678 }} onPick={() => {}} />);
    expect(screen.queryByLabelText("Remote object")).not.toBeInTheDocument();

    vi.mocked(clientToolApi.objectList).mockRejectedValue(new Error("no answer from 5678"));
    await user.click(screen.getByRole("button", { name: /browse remote device/i }));
    expect(await screen.findByText(/no answer from 5678/)).toBeInTheDocument();
    rerender(<RemotePointPicker deviceId={599001} target={{ instance: 1234 }} onPick={() => {}} />);
    expect(screen.queryByText(/no answer from 5678/)).not.toBeInTheDocument();
  });
});
