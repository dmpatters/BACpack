import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import LiveApplyBanners from "./LiveApplyBanners";
import type { DeviceProcessStatus } from "../api/types";

function status(fields: Partial<DeviceProcessStatus>): DeviceProcessStatus {
  return { device_id: 1, process_status: "running", apply_error: null, restart_pending: false, ...fields } as DeviceProcessStatus;
}

describe("LiveApplyBanners", () => {
  it("shows the apply error of a running device", () => {
    render(<LiveApplyBanners status={status({ apply_error: "object 'AV-1': bad units" })} />);
    expect(screen.getByTestId("apply-error")).toHaveTextContent(
      "The running device couldn't apply the latest changes: object 'AV-1': bad units. It keeps serving the previous config"
    );
  });

  it("asks for a restart only when device settings changed", () => {
    const { rerender } = render(<LiveApplyBanners status={status({})} />);
    expect(screen.queryByTestId("restart-pending")).not.toBeInTheDocument();
    rerender(<LiveApplyBanners status={status({ restart_pending: true })} />);
    expect(screen.getByTestId("restart-pending")).toHaveTextContent("Device settings changed. Restart serving to apply them.");
  });

  it("shows nothing for a stopped device", () => {
    const { container } = render(
      <LiveApplyBanners status={status({ process_status: "stopped", apply_error: "x", restart_pending: true })} />
    );
    expect(container).toBeEmptyDOMElement();
  });
});
