import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import DeviceNetworkRole from "./DeviceNetworkRole";
import { expectNamedControls } from "../testing/controls";
import { devicesApi, engineApi, settingsApi } from "../api/endpoints";
import type { DeviceSummary } from "../api/types";
import { GUIDE_URL } from "../utils/bind";

vi.mock("../api/endpoints", () => ({
  devicesApi: { list: vi.fn(), update: vi.fn() },
  settingsApi: { get: vi.fn() },
  engineApi: { status: vi.fn() },
}));

function summary(id: number, name: string, bind: string | null, extra: Partial<DeviceSummary> = {}): DeviceSummary {
  return {
    id,
    name,
    description: null,
    location: null,
    vendor_identifier: 999,
    vendor_name: null,
    object_count: 0,
    bind_address: bind,
    ...extra,
  };
}

const THIS = summary(599001, "D", "192.168.1.200/24:47810");
const OTHERS = [
  summary(599002, "E", "192.168.1.200/24:47811"),
  summary(599003, "F", "192.168.1.200/24:47812"),
  summary(599004, "Same", "192.168.1.221/24:47810"),
  summary(599005, "Remote", "192.168.1.200/24:47813", { network_role: "foreign" }),
];

function renderRole(device: DeviceSummary = THIS) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <DeviceNetworkRole deviceId={device.id} device={device} />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe("DeviceNetworkRole", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(devicesApi.list).mockResolvedValue([THIS, ...OTHERS]);
    vi.mocked(devicesApi.update).mockResolvedValue(THIS);
    vi.mocked(settingsApi.get).mockResolvedValue({ bind_address: null } as never);
    vi.mocked(engineApi.status).mockResolvedValue(
      [599002, 599003].map((device_id) => ({ device_id, process_status: "running" })) as never
    );
  });

  it("offers the three roles, Normal by default, and links the guide", async () => {
    renderRole();
    const select = await screen.findByLabelText("Network role");
    expect(select).toHaveValue("normal");
    expect(Array.from(select.querySelectorAll("option")).map((o) => o.value)).toEqual(["normal", "foreign", "bbmd"]);
    expect(screen.getByRole("link", { name: "Sharing the BACnet port" })).toHaveAttribute("href", GUIDE_URL);
  });

  it.each([
    ["normal", { foreign: false, bbmd: false }],
    ["foreign", { foreign: true, bbmd: false }],
    ["bbmd", { foreign: false, bbmd: true }],
  ] as const)("shows only the %s role's fields", async (role, shown) => {
    renderRole();
    await userEvent.setup().selectOptions(await screen.findByLabelText("Network role"), role);
    expect(!!screen.queryByLabelText("BBMD address")).toBe(shown.foreign);
    expect(!!screen.queryByLabelText("Registration lifetime (s)")).toBe(shown.foreign);
    expect(!!screen.queryByRole("group", { name: "Forward broadcasts to" })).toBe(shown.bbmd);
    expect(!!screen.queryByLabelText("Other BBMDs (typed)")).toBe(shown.bbmd);
  });

  it("lists the other devices by name, instance and address with port", async () => {
    renderRole({ ...THIS, network_role: "bbmd" });
    expect(await screen.findByRole("checkbox", { name: "E (599002) · 192.168.1.200/24 · UDP 47811" })).toBeEnabled();
    expect(screen.getByRole("checkbox", { name: "F (599003) · 192.168.1.200/24 · UDP 47812" })).toBeEnabled();
    expect(screen.queryByRole("checkbox", { name: /D \(599001\)/ })).not.toBeInTheDocument();
  });

  it("shows a device on this device's own network as such, and not tickable", async () => {
    renderRole({ ...THIS, network_role: "bbmd" });
    const same = await screen.findByRole("checkbox", { name: /Same \(599004\)/ });
    expect(same).toBeDisabled();
    expect(same).toHaveAccessibleDescription("already on this device's network");
  });

  it("judges a device on the server default by that address", async () => {
    vi.mocked(settingsApi.get).mockResolvedValue({ bind_address: "192.168.1.222/24:47810" } as never);
    vi.mocked(devicesApi.list).mockResolvedValue([THIS, summary(599006, "OnDefault", null)]);
    renderRole({ ...THIS, network_role: "bbmd" });
    const onDefault = await screen.findByRole("checkbox", {
      name: "OnDefault (599006) · 192.168.1.222/24 · UDP 47810 (default)",
    });
    expect(onDefault).toBeDisabled();
    expect(onDefault).toHaveAccessibleDescription("already on this device's network");
  });

  it("says a linked device is stopped, so it can't answer", async () => {
    vi.mocked(engineApi.status).mockResolvedValue([
      { device_id: 599002, process_status: "running" },
      { device_id: 599003, process_status: "stopped" },
    ] as never);
    renderRole({ ...THIS, network_role: "bbmd", bbmd_links: [599002, 599003] });
    const stopped = await screen.findByRole("checkbox", { name: /F \(599003\)/ });
    expect(stopped).toBeEnabled();
    await waitFor(() => expect(stopped).toHaveAccessibleDescription("stopped"));
    expect(screen.getByRole("checkbox", { name: /E \(599002\)/ })).not.toHaveAccessibleDescription();
  });

  it("disables a foreign device and says why", async () => {
    renderRole({ ...THIS, network_role: "bbmd" });
    const remote = await screen.findByRole("checkbox", { name: /Remote \(599005\)/ });
    expect(remote).toBeDisabled();
    expect(remote).toHaveAccessibleDescription("a foreign device can't be linked");
  });

  it("saves all five fields, the ticked devices as links", async () => {
    const user = userEvent.setup();
    renderRole({ ...THIS, network_role: "bbmd", bbmd_peers: ["10.0.0.9:47808"], bbmd_links: [599003] });

    await user.click(await screen.findByRole("checkbox", { name: /E \(599002\)/ }));
    await user.click(screen.getByRole("button", { name: "Save network role" }));

    expect(devicesApi.update).toHaveBeenCalledWith(599001, {
      network_role: "bbmd",
      foreign_bbmd: null,
      foreign_ttl: 60,
      bbmd_peers: ["10.0.0.9:47808"],
      bbmd_links: [599002, 599003],
    });
    expect(await screen.findByText("Network role saved. Applies on start.")).toBeInTheDocument();
  });

  it("saves a foreign device's BBMD address and lifetime", async () => {
    const user = userEvent.setup();
    renderRole();
    await user.selectOptions(await screen.findByLabelText("Network role"), "foreign");
    await user.type(screen.getByLabelText("BBMD address"), "192.168.1.5:47808");
    await user.clear(screen.getByLabelText("Registration lifetime (s)"));
    await user.type(screen.getByLabelText("Registration lifetime (s)"), "120");
    await user.click(screen.getByRole("button", { name: "Save network role" }));

    expect(devicesApi.update).toHaveBeenCalledWith(
      599001,
      expect.objectContaining({ network_role: "foreign", foreign_bbmd: "192.168.1.5:47808", foreign_ttl: 120 })
    );
  });

  it("shows a refused save", async () => {
    vi.mocked(devicesApi.update).mockRejectedValue(new Error("E (599002) is a foreign device; it can't be linked"));
    const user = userEvent.setup();
    renderRole();
    await user.click(await screen.findByRole("button", { name: "Save network role" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn't save the network role: E (599002) is a foreign device; it can't be linked"
    );
  });

  it("names every control", async () => {
    const { container } = renderRole({ ...THIS, network_role: "bbmd" });
    await screen.findByRole("checkbox", { name: /E \(599002\)/ });
    expectNamedControls(container);
  });
});
