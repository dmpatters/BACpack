import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { ComponentProps } from "react";
import DeviceCard from "./DeviceCard";
import type { DeviceSummary, DeviceProcessStatus } from "../api/types";

const device: DeviceSummary = {
  id: 1,
  name: "Test Device",
  description: null,
  location: null,
  vendor_identifier: 999,
  vendor_name: null,
  object_count: 3,
  bind_address: null,
};

function renderCard(props: Partial<ComponentProps<typeof DeviceCard>> = {}) {
  return render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <DeviceCard device={device} status={undefined} busy={false} onStart={vi.fn()} onStop={vi.fn()} {...props} />
    </MemoryRouter>
  );
}

// The value beside a term in the card's meta list.
function meta(term: string): string | null | undefined {
  const dt = screen.getAllByRole("term").find((t) => t.textContent === term);
  return dt?.nextElementSibling?.textContent;
}

describe("DeviceCard", () => {
  it("shows Start and calls onStart when the device is stopped", async () => {
    const user = userEvent.setup();
    const onStart = vi.fn();
    renderCard({ onStart });

    await user.click(screen.getByRole("button", { name: "Start" }));
    expect(onStart).toHaveBeenCalled();
  });

  it("shows Stop, the pid, and calls onStop when the device is running", async () => {
    const user = userEvent.setup();
    const onStop = vi.fn();
    const status: DeviceProcessStatus = {
      device_id: 1,
      process_status: "running",
      process_pid: 4242,
      process_started_at: "2026-09-08T00:00:00",
      process_error: null,
      bind_address: "192.168.1.50/24",
      dcc_state: "enable",
      database_revision: 1,
      last_restore_time: null,
      clock_offset_seconds: 0,
      clock_change_pending: false,
      device_time: "2026-09-25T14:03:12-04:00",
    };
    renderCard({ status, onStop });

    expect(screen.getByText("4242")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Stop" }));
    expect(onStop).toHaveBeenCalled();
  });

  it("disables its action button while busy", () => {
    renderCard({ busy: true });
    expect(screen.getByRole("button", { name: "Start" })).toBeDisabled();
  });

  // The error text lives on the device page; the card only says crashed.
  it("shows crashed as a pill, without the error text", () => {
    const status: DeviceProcessStatus = {
      device_id: 1,
      process_status: "crashed",
      process_pid: null,
      process_started_at: null,
      process_error: "exit code 1",
      bind_address: null,
      dcc_state: "enable",
      database_revision: 1,
      last_restore_time: null,
      clock_offset_seconds: 0,
      clock_change_pending: false,
      device_time: "2026-09-25T14:03:12-04:00",
    };
    renderCard({ status });
    expect(screen.getByText("crashed")).toHaveClass("bp-pill--danger");
    expect(screen.queryByText("exit code 1")).not.toBeInTheDocument();
  });

  it("shows the server default, marked, for a device without its own address", () => {
    renderCard({ device: { ...device, bind_address: null }, defaultBind: "192.168.1.140/24" });
    expect(meta("bind")).toBe("192.168.1.140/24 (default)");
    expect(meta("port")).toBe("47808");
  });

  it("points a device whose port is taken at its Process tab", () => {
    renderCard({
      status: {
        device_id: 1,
        process_status: "crashed",
        process_pid: null,
        process_error: "UDP port 192.168.1.140:47808 is already in use",
        port_in_use: true,
      } as DeviceProcessStatus,
    });
    expect(screen.getByRole("link", { name: "Port in use: pick another" })).toHaveAttribute("href", "/devices/1/process");
  });

  it("has no port pointer for a crash with another cause", () => {
    renderCard({
      status: { device_id: 1, process_status: "crashed", process_pid: null, process_error: "boom" } as DeviceProcessStatus,
    });
    expect(screen.queryByRole("link", { name: /Port in use/ })).not.toBeInTheDocument();
  });

  it("says default when no bind address is configured", () => {
    renderCard({ device: { ...device, bind_address: null } });
    expect(meta("bind")).toBe("default");
    expect(meta("port")).toBe("—");
  });

  it("shows the meta as a list and one action", () => {
    renderCard({
      device: { ...device, id: 599002, object_count: 55, bind_address: "192.168.1.141/24" },
      status: { device_id: 599002, process_status: "running", process_pid: 11679 } as DeviceProcessStatus,
    });
    expect(screen.getByRole("article")).toHaveClass("bp-device");
    expect(screen.getAllByRole("term").map((t) => t.textContent)).toEqual(["device", "objects", "pid", "bind", "port"]);
    expect(screen.getByText("55")).toBeInTheDocument();
    expect(screen.getByText("11679")).toBeInTheDocument();
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual(["Stop"]);
  });

  it("shows an em dash for the pid of a stopped device and a primary Start", () => {
    renderCard();
    expect(meta("pid")).toBe("—");
    expect(screen.getByRole("button", { name: "Start" })).toHaveClass("bp-btn--primary");
  });

  it("shows the device's own bind address when configured", () => {
    renderCard({ device: { ...device, bind_address: "192.168.1.50/24" } });
    expect(meta("bind")).toBe("192.168.1.50/24");
    expect(meta("port")).toBe("47808");
  });

  it("shows an explicit UDP port", () => {
    renderCard({ device: { ...device, bind_address: "192.168.1.50/24:47810" } });
    expect(meta("bind")).toBe("192.168.1.50/24");
    expect(meta("port")).toBe("47810");
  });

  it("disables the action button while the device is starting or stopping", () => {
    const startingStatus: DeviceProcessStatus = {
      device_id: 1,
      process_status: "starting",
      process_pid: null,
      process_started_at: null,
      process_error: null,
      bind_address: null,
      dcc_state: "enable",
      database_revision: 1,
      last_restore_time: null,
      clock_offset_seconds: 0,
      clock_change_pending: false,
      device_time: "2026-09-25T14:03:12-04:00",
    };
    renderCard({ status: startingStatus });
    expect(screen.getByRole("button", { name: "Stop" })).toBeDisabled();
  });
  it("shows a disabled Stop while the device is reinitializing", () => {
    renderCard({
      status: { device_id: 1, process_status: "reinitializing", process_pid: 123, process_error: null } as DeviceProcessStatus,
    });
    expect(screen.getByRole("button", { name: "Stop" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Start" })).not.toBeInTheDocument();
  });

  it("shows pending work as pills beside the state, never as buttons", () => {
    renderCard({
      status: { device_id: 1, process_status: "running", process_pid: 4242, restart_pending: true, apply_error: "row 3" } as DeviceProcessStatus,
    });
    expect(screen.getByText("running")).toHaveClass("bp-pill--ok");
    expect(screen.getByText("restart pending")).toHaveClass("bp-pill--warn");
    expect(screen.getByText("apply failed")).toHaveClass("bp-pill--danger");
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual(["Stop"]);
    expect(screen.queryByText("row 3")).not.toBeInTheDocument();
  });
});
