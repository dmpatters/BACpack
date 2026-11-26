import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi, beforeEach } from "vitest";
import DeviceSettingsTab from "./DeviceSettingsTab";
import { expectNamedControls } from "../testing/controls";
import { columnOf, inSettingsColumn } from "../testing/columns";
import { devicesApi, engineApi } from "../api/endpoints";
import type { DeviceProcessStatus, DeviceSummary } from "../api/types";

vi.mock("../api/endpoints", () => ({
  devicesApi: { get: vi.fn(), update: vi.fn() },
  engineApi: { status: vi.fn(), restart: vi.fn() },
}));

function statusOf(process_status: string) {
  return [{ device_id: 599002, process_status } as DeviceProcessStatus];
}

const device: DeviceSummary = {
  id: 599002,
  name: "Bugfix Session Device",
  description: "5 of every object type",
  location: "Lab rack 2",
  vendor_identifier: 999,
  vendor_name: null,
  bind_address: "192.168.1.141/24",
  object_count: 54,
  backup_failure_timeout: 60,
} as DeviceSummary;

function renderTab() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <DeviceSettingsTab deviceId={599002} />
    </QueryClientProvider>
  );
}

describe("DeviceSettingsTab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(devicesApi.get).mockResolvedValue(device);
    vi.mocked(engineApi.status).mockResolvedValue(statusOf("running"));
  });

  it("exposes the identity fields that previously had no UI at all", async () => {
    renderTab();
    expect(await screen.findByLabelText(/^name/i)).toHaveValue("Bugfix Session Device");
    expect(screen.getByLabelText(/description/i)).toHaveValue("5 of every object type");
    expect(screen.getByLabelText(/vendor identifier/i)).toHaveValue(999);
    expect(screen.getByLabelText(/vendor name/i)).toBeInTheDocument();
  });

  it("stacks Identity, Vendor and BACnet password in the settings column, with one Save under all", async () => {
    renderTab();

    const identity = await screen.findByRole("group", { name: "Identity" });
    for (const panel of [identity, screen.getByRole("group", { name: "Vendor" }), screen.getByRole("group", { name: "BACnet password" })]) {
      expect(inSettingsColumn(panel)).toBe(true);
      expect(columnOf(panel)).toBeNull();
    }
    expect(inSettingsColumn(screen.getByRole("button", { name: /^save$/i }))).toBe(true);
  });

  it("shows and saves the device location", async () => {
    const user = userEvent.setup();
    vi.mocked(devicesApi.update).mockResolvedValue(device);
    renderTab();

    const field = await screen.findByLabelText(/location/i);
    expect(field).toHaveValue("Lab rack 2");
    await user.tripleClick(field);
    await user.keyboard("Roof AHU-3");
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    expect(devicesApi.update).toHaveBeenCalledWith(599002, expect.objectContaining({ location: "Roof AHU-3" }));
  });

  it("sends null when the location is cleared", async () => {
    const user = userEvent.setup();
    vi.mocked(devicesApi.update).mockResolvedValue(device);
    renderTab();

    await user.clear(await screen.findByLabelText(/location/i));
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    expect(devicesApi.update).toHaveBeenCalledWith(599002, expect.objectContaining({ location: null }));
  });

  it("shows what a client sees when vendor name is unset", async () => {
    renderTab();
    expect(await screen.findByLabelText(/vendor name/i)).toHaveAttribute("placeholder", "BACpack");
  });

  it("allows a vendor identifier of 0 and bounds it at 65535", async () => {
    renderTab();
    const field = await screen.findByLabelText(/vendor identifier/i);
    expect(field).toHaveAttribute("min", "0");
    expect(field).toHaveAttribute("max", "65535");
  });

  it("saves a change to another field when the vendor identifier is 0", async () => {
    const user = userEvent.setup();
    vi.mocked(devicesApi.update).mockResolvedValue(device);
    vi.mocked(devicesApi.get).mockResolvedValue({ ...device, vendor_identifier: 0 } as DeviceSummary);
    renderTab();
    const description = await screen.findByLabelText(/description/i);
    await user.clear(description);
    await user.type(description, "changed");
    await user.click(screen.getByRole("button", { name: /save/i }));
    expect(devicesApi.update).toHaveBeenCalledWith(599002, { description: "changed" });
  });

  it("sends vendor identifier 0", async () => {
    const user = userEvent.setup();
    vi.mocked(devicesApi.update).mockResolvedValue(device);
    renderTab();
    const field = await screen.findByLabelText(/vendor identifier/i);
    await user.clear(field);
    await user.type(field, "0");
    await user.click(screen.getByRole("button", { name: /save/i }));
    expect(devicesApi.update).toHaveBeenCalledWith(599002, { vendor_identifier: 0 });
  });

  it("sends only the fields that changed", async () => {
    // Sending the rest would put back a name or description a BACnet client
    // wrote since this tab loaded.
    const user = userEvent.setup();
    vi.mocked(devicesApi.update).mockResolvedValue(device);
    renderTab();

    await user.type(await screen.findByLabelText(/vendor name/i), "Example Controls");
    await user.click(screen.getByRole("button", { name: /save/i }));

    expect(devicesApi.update).toHaveBeenCalledWith(599002, { vendor_name: "Example Controls" });
  });

  it("sends null rather than an empty string when vendor name is cleared", async () => {
    const user = userEvent.setup();
    vi.mocked(devicesApi.update).mockResolvedValue(device);
    vi.mocked(devicesApi.get).mockResolvedValue({ ...device, vendor_name: "Old Co" } as DeviceSummary);
    renderTab();

    await user.clear(await screen.findByLabelText(/vendor name/i));
    await user.click(screen.getByRole("button", { name: /save/i }));

    expect(devicesApi.update).toHaveBeenCalledWith(
      599002,
      expect.objectContaining({ vendor_name: null })
    );
  });

  it("shows and saves the backup failure timeout", async () => {
    const user = userEvent.setup();
    vi.mocked(devicesApi.update).mockResolvedValue(device);
    renderTab();

    const field = await screen.findByLabelText("Backup failure timeout (seconds)");
    expect(field).toHaveValue(60);
    await user.clear(field);
    await user.type(field, "15");
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    expect(devicesApi.update).toHaveBeenCalledWith(599002, { backup_failure_timeout: 15 });
  });

  it.each([["blank", ""], ["zero", "0"], ["too large", "65536"]])(
    "refuses a %s backup failure timeout",
    async (_what, typed) => {
      const user = userEvent.setup();
      renderTab();

      const field = await screen.findByLabelText("Backup failure timeout (seconds)");
      await user.clear(field);
      if (typed) fireEvent.change(field, { target: { value: typed } });
      await user.click(screen.getByRole("button", { name: /^save$/i }));

      expect(devicesApi.update).not.toHaveBeenCalled();
      expect(field).toBeInvalid();
    }
  );

  it("says which settings apply live after a save", async () => {
    const user = userEvent.setup();
    vi.mocked(devicesApi.update).mockResolvedValue(device);
    renderTab();

    await user.click(await screen.findByRole("button", { name: /^save$/i }));

    expect(
      await screen.findByText(
        "Saved. Name and vendor apply after a restart."
      )
    ).toBeInTheDocument();
  });

  it("shows the restart-pending banner from the device status", async () => {
    vi.mocked(engineApi.status).mockResolvedValue([
      { device_id: 599002, process_status: "running", restart_pending: true, apply_error: null } as DeviceProcessStatus,
    ]);
    renderTab();
    expect(await screen.findByTestId("restart-pending")).toBeInTheDocument();
  });

  it("restarts the device from this tab while it is running", async () => {
    const user = userEvent.setup();
    vi.mocked(engineApi.restart).mockResolvedValue(statusOf("running"));
    renderTab();

    await user.click(await screen.findByRole("button", { name: "Restart serving" }));

    expect(engineApi.restart).toHaveBeenCalledWith(599002);
    expect(await screen.findByText("Restarted.")).toBeInTheDocument();
  });

  it("has no restart button while the device is stopped", async () => {
    vi.mocked(engineApi.status).mockResolvedValue(statusOf("stopped"));
    renderTab();

    await screen.findByLabelText(/^name/i);
    expect(screen.queryByRole("button", { name: "Restart serving" })).not.toBeInTheDocument();
  });

  it("shows the save result under Save, not at the top", async () => {
    vi.mocked(devicesApi.update).mockRejectedValue(new Error("name already used"));
    const user = userEvent.setup();
    renderTab();
    await user.type(await screen.findByLabelText("Name"), " 2");
    await user.click(screen.getByRole("button", { name: "Save" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Couldn't save: name already used");
    expect(screen.getByRole("button", { name: "Save" }).closest("form")).toContainElement(alert);
  });

  it("names every control", async () => {
    const { container } = renderTab();
    await screen.findByLabelText("Name");
    expectNamedControls(container);
  });

  it("keeps an unsaved edit when another tab's save refreshes the device", async () => {
    const user = userEvent.setup();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <DeviceSettingsTab deviceId={599002} />
      </QueryClientProvider>
    );
    await user.type(await screen.findByLabelText("Description"), " edited");
    vi.mocked(devicesApi.get).mockResolvedValue({ ...device, location: "Roof" });
    await client.invalidateQueries({ queryKey: ["device", 599002] });
    await waitFor(() => expect(screen.getByLabelText("Location")).toHaveValue("Roof"));
    expect(screen.getByLabelText("Description")).toHaveValue("5 of every object type edited");
  });

  it("counts a confirmed save as saved before the refetch lands", async () => {
    const user = userEvent.setup();
    vi.mocked(devicesApi.update).mockResolvedValue(device);
    renderTab();
    await user.type(await screen.findByLabelText("Description"), " edited");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Saved.");
  });

  describe("BACnet password", () => {
    const label = "Password for DeviceCommunicationControl and ReinitializeDevice";

    it("says Not set when the device has no BACnet password", async () => {
      renderTab();
      const field = await screen.findByLabelText(label);
      expect(field).toHaveValue("");
      expect(field).toHaveAccessibleDescription(/^Not set\./);
      expect(screen.queryByRole("button", { name: "Clear password" })).not.toBeInTheDocument();
    });

    it("says Set, leaves the field empty and offers Clear password when one is set", async () => {
      vi.mocked(devicesApi.get).mockResolvedValue({ ...device, bacnet_password_set: true });
      renderTab();
      const field = await screen.findByLabelText(label);
      expect(field).toHaveValue("");
      expect(field).toHaveAccessibleDescription(/^Set\./);
      expect(screen.getByRole("button", { name: "Clear password" })).toBeInTheDocument();
    });

    it("limits the field to 20 characters", async () => {
      renderTab();
      expect(await screen.findByLabelText(label)).toHaveAttribute("maxlength", "20");
    });

    it("sends bacnet_password only when something was typed", async () => {
      const user = userEvent.setup();
      vi.mocked(devicesApi.update).mockResolvedValue(device);
      renderTab();
      await user.type(await screen.findByLabelText(label), "secret");
      await user.click(screen.getByRole("button", { name: /^save$/i }));
      expect(devicesApi.update).toHaveBeenCalledWith(599002, { bacnet_password: "secret" });
    });

    it("does not send the password with an unrelated change", async () => {
      const user = userEvent.setup();
      vi.mocked(devicesApi.update).mockResolvedValue(device);
      renderTab();
      await user.type(await screen.findByLabelText("Vendor name"), "X");
      await user.click(screen.getByRole("button", { name: /^save$/i }));
      expect(vi.mocked(devicesApi.update).mock.calls[0][1]).not.toHaveProperty("bacnet_password");
    });

    it("Clear password sends null on Save", async () => {
      const user = userEvent.setup();
      vi.mocked(devicesApi.get).mockResolvedValue({ ...device, bacnet_password_set: true });
      vi.mocked(devicesApi.update).mockResolvedValue({ ...device, bacnet_password_set: false });
      renderTab();
      await user.click(await screen.findByRole("button", { name: "Clear password" }));
      expect(screen.getByLabelText(label)).toHaveAccessibleDescription("Will be cleared when you save.");
      expect(screen.queryByRole("button", { name: "Clear password" })).not.toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: /^save$/i }));
      expect(devicesApi.update).toHaveBeenCalledWith(599002, { bacnet_password: null });
    });

    it("typing after Clear password sets the new password instead", async () => {
      const user = userEvent.setup();
      vi.mocked(devicesApi.get).mockResolvedValue({ ...device, bacnet_password_set: true });
      vi.mocked(devicesApi.update).mockResolvedValue(device);
      renderTab();
      await user.click(await screen.findByRole("button", { name: "Clear password" }));
      await user.type(screen.getByLabelText(label), "fresh");
      await user.click(screen.getByRole("button", { name: /^save$/i }));
      expect(devicesApi.update).toHaveBeenCalledWith(599002, { bacnet_password: "fresh" });
    });

    it("goes back to clearing, not keeping, when a typed character is deleted again", async () => {
      const user = userEvent.setup();
      vi.mocked(devicesApi.get).mockResolvedValue({ ...device, bacnet_password_set: true });
      vi.mocked(devicesApi.update).mockResolvedValue({ ...device, bacnet_password_set: false });
      renderTab();
      await user.click(await screen.findByRole("button", { name: "Clear password" }));
      const field = screen.getByLabelText(label);
      await user.type(field, "a");
      expect(field).toHaveAccessibleDescription("Will be replaced when you save.");
      await user.clear(field);
      expect(field).toHaveAccessibleDescription("Will be cleared when you save.");
      expect(screen.queryByRole("button", { name: "Clear password" })).not.toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: /^save$/i }));
      expect(devicesApi.update).toHaveBeenCalledWith(599002, { bacnet_password: null });
    });

    it("offers Keep password only after Clear password", async () => {
      const user = userEvent.setup();
      vi.mocked(devicesApi.get).mockResolvedValue({ ...device, bacnet_password_set: true });
      renderTab();
      await user.click(await screen.findByRole("button", { name: "Clear password" }));
      expect(screen.getByRole("button", { name: "Keep password" })).toBeInTheDocument();
    });

    it("has no Keep password before Clear password is clicked", async () => {
      vi.mocked(devicesApi.get).mockResolvedValue({ ...device, bacnet_password_set: true });
      renderTab();
      await screen.findByRole("button", { name: "Clear password" });
      expect(screen.queryByRole("button", { name: "Keep password" })).not.toBeInTheDocument();
    });

    it("Keep password undoes Clear password and Save leaves the password alone", async () => {
      const user = userEvent.setup();
      vi.mocked(devicesApi.get).mockResolvedValue({ ...device, bacnet_password_set: true });
      vi.mocked(devicesApi.update).mockResolvedValue({ ...device, bacnet_password_set: true });
      renderTab();
      await user.click(await screen.findByRole("button", { name: "Clear password" }));
      await user.click(screen.getByRole("button", { name: "Keep password" }));
      expect(screen.queryByRole("button", { name: "Keep password" })).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Clear password" })).toBeInTheDocument();
      expect(screen.getByLabelText(label)).toHaveAccessibleDescription(/^Set\./);
      await user.type(screen.getByLabelText("Vendor name"), "X");
      await user.click(screen.getByRole("button", { name: /^save$/i }));
      expect(vi.mocked(devicesApi.update).mock.calls[0][1]).not.toHaveProperty("bacnet_password");
    });

    it("Keep password empties a typed replacement and Save sends no password", async () => {
      const user = userEvent.setup();
      vi.mocked(devicesApi.get).mockResolvedValue({ ...device, bacnet_password_set: true });
      vi.mocked(devicesApi.update).mockResolvedValue({ ...device, bacnet_password_set: true });
      renderTab();
      await user.click(await screen.findByRole("button", { name: "Clear password" }));
      await user.type(screen.getByLabelText(label), "fresh");
      await user.click(screen.getByRole("button", { name: "Keep password" }));
      expect(screen.getByLabelText(label)).toHaveValue("");
      await user.type(screen.getByLabelText("Vendor name"), "X");
      await user.click(screen.getByRole("button", { name: /^save$/i }));
      expect(vi.mocked(devicesApi.update).mock.calls[0][1]).not.toHaveProperty("bacnet_password");
    });

    it("Keep password leaves the form clean when nothing else changed", async () => {
      const user = userEvent.setup();
      vi.mocked(devicesApi.get).mockResolvedValue({ ...device, bacnet_password_set: true });
      vi.mocked(devicesApi.update).mockResolvedValue({ ...device, bacnet_password_set: true });
      renderTab();
      await user.type(await screen.findByLabelText("Vendor name"), "X");
      await user.click(screen.getByRole("button", { name: /^save$/i }));
      expect(await screen.findByRole("status")).toHaveTextContent("Saved.");
      await user.click(screen.getByRole("button", { name: "Clear password" }));
      expect(screen.queryByRole("status")).not.toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: "Keep password" }));
      expect(await screen.findByRole("status")).toHaveTextContent("Saved.");
    });

    it("empties the field after a successful save", async () => {
      const user = userEvent.setup();
      vi.mocked(devicesApi.update).mockResolvedValue({ ...device, bacnet_password_set: true });
      renderTab();
      const field = await screen.findByLabelText(label);
      await user.type(field, "secret");
      await user.click(screen.getByRole("button", { name: /^save$/i }));
      await waitFor(() => expect(field).toHaveValue(""));
    });
  });
});
