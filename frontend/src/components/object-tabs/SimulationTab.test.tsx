import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import SimulationTab from "./SimulationTab";
import { TabPanels } from "../Tabs";
import { expectNamedControls } from "../../testing/controls";
import { liveApi, objectsApi } from "../../api/endpoints";
import { ApiError } from "../../api/http";
import type { BacnetObjectDTO } from "../../api/types";

vi.mock("../../api/endpoints", () => ({ objectsApi: { update: vi.fn() }, liveApi: { object: vi.fn() } }));

function makeObj(overrides: Partial<BacnetObjectDTO> = {}): BacnetObjectDTO {
  return {
    id: 5,
    device_id: 599002,
    object_type: "analog-value",
    instance_number: 1,
    name: "AV-1",
    units: null,
    state_text: null,
    simulation: null,
    fed_by_connector_id: null,
    fed_by_connector_name: null,
    ...overrides,
  } as BacnetObjectDTO;
}

function renderTab(obj: BacnetObjectDTO) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <SimulationTab deviceId={599002} obj={obj} />
    </QueryClientProvider>
  );
}

describe("SimulationTab", () => {
  beforeEach(() => vi.mocked(objectsApi.update).mockReset().mockResolvedValue(makeObj() as never));

  it("offers only the kinds for the object's type", () => {
    renderTab(makeObj({ object_type: "binary-value" }));
    const kind = screen.getByRole("radiogroup", { name: "Kind" });
    expect(within(kind).getAllByRole("radio").map((r) => r.closest("label")?.textContent)).toEqual(["Square", "Random"]);
    expect(within(kind).getByRole("radio", { name: "Square" })).toBeDisabled();
  });

  it("shows the fields disabled while off, and enables them with the checkbox", async () => {
    const user = userEvent.setup();
    const { container } = renderTab(makeObj());
    expect(container.querySelector(".bp-sim")).toHaveClass("bp-sim--off");
    expect(screen.getByLabelText("Center")).toBeDisabled();
    await user.click(screen.getByLabelText("Simulate this object"));
    expect(container.querySelector(".bp-sim")).not.toHaveClass("bp-sim--off");
    expect(screen.getByLabelText("Center")).toBeEnabled();
    expect(screen.getByLabelText("Simulate this object")).toHaveAccessibleDescription("At priority 10.");
  });

  it("previews the shape before saving, on a UTC clock", async () => {
    const user = userEvent.setup();
    renderTab(makeObj({ units: "degreesFahrenheit" }));
    await user.click(screen.getByLabelText("Simulate this object"));
    const preview = screen.getByRole("region", { name: /Preview/ });
    expect(within(preview).getByText("not live")).toBeInTheDocument();
    expect(within(preview).getByText("next 20 min · °F")).toBeInTheDocument();
    expect(within(preview).getByRole("img")).toHaveAccessibleName(/Sine/);
    expect(within(preview).getByText("Sine · swings 40 – 60 every 10 min")).toBeInTheDocument();
    expect(within(preview).getByText("Times in UTC")).toBeInTheDocument();
    expect(screen.getByLabelText("Center (°F)")).toHaveValue(50);
  });

  it("keeps the last good preview, dimmed, while a field is wrong, and won't save it", async () => {
    const user = userEvent.setup();
    renderTab(makeObj());
    await user.click(screen.getByLabelText("Simulate this object"));
    await user.selectOptions(screen.getByLabelText("Kind"), "Triangle");
    const preview = screen.getByRole("region", { name: /Preview/ });
    // user.clear doesn't empty a number input in jsdom.
    fireEvent.change(screen.getByLabelText("High"), { target: { value: "-5" } });
    expect(screen.getByLabelText("High")).toHaveAccessibleDescription("High must be above low.");
    expect(preview).toHaveClass("bp-chart--stale");
    expect(within(preview).getByText(/ramps 0 – 100/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(screen.getByText("Can't save:").closest(".bp-error")).toHaveTextContent("Can't save: High must be above low.");
    expect(objectsApi.update).not.toHaveBeenCalled();
  });

  it("drops the Can't save message once the problem is fixed", async () => {
    const user = userEvent.setup();
    renderTab(makeObj());
    await user.click(screen.getByLabelText("Simulate this object"));
    await user.selectOptions(screen.getByLabelText("Kind"), "Triangle");
    fireEvent.change(screen.getByLabelText("High"), { target: { value: "-5" } });
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(screen.getByText("Can't save:")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("High"), { target: { value: "100" } });
    expect(screen.queryByText("Can't save:")).not.toBeInTheDocument();
  });

  it("keeps the last good preview after a wrong field is left behind by turning simulation off", async () => {
    const user = userEvent.setup();
    const { container } = renderTab(makeObj());
    await user.click(screen.getByLabelText("Simulate this object"));
    await user.selectOptions(screen.getByLabelText("Kind"), "Triangle");
    fireEvent.change(screen.getByLabelText("High"), { target: { value: "-5" } });
    await user.click(screen.getByLabelText("Simulate this object"));
    const preview = screen.getByRole("region", { name: /Preview/ });
    expect(within(preview).getByText(/ramps 0 – 100/)).toBeInTheDocument();
    expect(container.innerHTML).not.toMatch(/NaN|Infinity/);
  });

  it("changing the kind keeps what the kinds share", async () => {
    const user = userEvent.setup();
    renderTab(makeObj());
    await user.click(screen.getByLabelText("Simulate this object"));
    fireEvent.change(screen.getByLabelText("Update every (s)"), { target: { value: "10" } });
    await user.selectOptions(screen.getByLabelText("Kind"), "Noise");
    expect(screen.getByLabelText("Update every (s)")).toHaveValue(10);
    expect(screen.queryByLabelText("Period (s)")).not.toBeInTheDocument();
  });

  it("resets the kind's fields without saving", async () => {
    const user = userEvent.setup();
    renderTab(makeObj());
    await user.click(screen.getByLabelText("Simulate this object"));
    fireEvent.change(screen.getByLabelText("Center"), { target: { value: "70" } });
    await user.click(screen.getByRole("button", { name: "Reset to defaults" }));
    expect(screen.getByLabelText("Center")).toHaveValue(50);
    expect(objectsApi.update).not.toHaveBeenCalled();
  });

  it("follows a source chosen from this device's analog objects", async () => {
    const user = userEvent.setup();
    const zone = makeObj({ id: 6, instance_number: 3, name: "Zone temp" });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <SimulationTab deviceId={599002} obj={makeObj()} objects={[makeObj(), zone, makeObj({ id: 7, object_type: "binary-value", name: "Pump" })]} />
      </QueryClientProvider>
    );
    await user.click(screen.getByLabelText("Simulate this object"));
    await user.selectOptions(screen.getByLabelText("Kind"), "Follow another object");
    await user.click(screen.getByRole("combobox", { name: "Source object" }));
    expect(within(screen.getByRole("listbox")).getAllByRole("option").map((o) => o.textContent)).toEqual(["analog-value:3 · Zone temp"]);
    await user.keyboard("{ArrowDown}{Enter}");
    const preview = screen.getByRole("region", { name: /Preview/ });
    expect(within(preview).getByText("Writes nothing while the source is missing")).toBeInTheDocument();
    expect(within(preview).queryByRole("img")).not.toBeInTheDocument();
  });

  async function followZone() {
    const user = userEvent.setup();
    const zone = makeObj({ id: 6, instance_number: 3, name: "Zone temp", units: "degreesFahrenheit" });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <SimulationTab deviceId={599002} obj={makeObj()} objects={[makeObj(), zone]} />
      </QueryClientProvider>
    );
    await user.click(screen.getByLabelText("Simulate this object"));
    await user.selectOptions(screen.getByLabelText("Kind"), "Follow another object");
    await user.click(screen.getByRole("combobox", { name: "Source object" }));
    await user.keyboard("{ArrowDown}{Enter}");
    return screen.getByRole("region", { name: /Preview/ });
  }

  it("shows the followed object's live value", async () => {
    vi.mocked(liveApi.object).mockResolvedValue({
      object_id: 6, object_type: "analog-value", instance: 3, present_value: 21.4, priority_array: null,
      controlling_priority: null, relinquish_default: null, status_flags: [], event_state: "normal",
      reliability: null, out_of_service: false, read_at: "2026-10-16T10:48:02+00:00",
    });
    const preview = await followZone();
    expect(await within(preview).findByText("Zone temp now reads 21.4 °F · updated 10:48:02 UTC")).toBeInTheDocument();
    expect(vi.mocked(liveApi.object)).toHaveBeenCalledWith(599002, 6);
  });

  it("says the source has no live value while its device is stopped", async () => {
    vi.mocked(liveApi.object).mockRejectedValue(new ApiError("device 599002 isn't running", 409));
    const preview = await followZone();
    expect(await within(preview).findByText("Zone temp: no live value: device not running")).toBeInTheDocument();
  });

  describe("on the clock", () => {
    const SINE = { kind: "sine", center: 50, amplitude: 10, period_s: 600, interval_s: 5, decimals: 2 };
    const FOLLOW = { kind: "follow", source: "analog-value:3", scale: 1, offset: 0, interval_s: 5 };
    const zone = makeObj({ id: 6, instance_number: 3, name: "Zone temp", units: "degreesFahrenheit" });
    const LIVE_ZONE = {
      object_id: 6, object_type: "analog-value", instance: 3, present_value: 21.4, priority_array: null,
      controlling_priority: null, relinquish_default: null, status_flags: [], event_state: "normal",
      reliability: null, out_of_service: false, read_at: "2026-10-16T10:00:00+00:00",
    };

    beforeEach(() => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      vi.setSystemTime(new Date("2026-10-16T10:00:00Z"));
    });
    afterEach(() => vi.useRealTimers());

    function renderInTabs(obj: BacnetObjectDTO, active: string) {
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      const tree = (tab: string) => (
        <QueryClientProvider client={client}>
          <TabPanels
            active={tab}
            panels={[
              { key: "sim", render: () => <SimulationTab deviceId={599002} obj={obj} objects={[obj, zone]} /> },
              { key: "other", render: () => <p>other tab</p> },
            ]}
          />
        </QueryClientProvider>
      );
      const view = render(tree(active));
      return { show: (tab: string) => view.rerender(tree(tab)) };
    }

    const advance = (ms: number) =>
      act(async () => {
        await vi.advanceTimersByTimeAsync(ms);
      });

    it("moves the preview's now on while the tab stays open", async () => {
      renderInTabs(makeObj({ simulation: SINE }), "sim");
      expect(within(screen.getByRole("img")).getByText("10:00")).toBeInTheDocument();
      await advance(60 * 60 * 1000);
      expect(within(screen.getByRole("img")).getByText("11:00")).toBeInTheDocument();
    });

    it("starts the preview from now when its tab is shown again", async () => {
      const tabs = renderInTabs(makeObj({ simulation: SINE }), "sim");
      tabs.show("other");
      vi.setSystemTime(new Date("2026-10-16T13:00:00Z"));
      tabs.show("sim");
      expect(within(screen.getByRole("img")).getByText("13:00")).toBeInTheDocument();
    });

    it("stops polling the followed object while its tab is hidden", async () => {
      vi.mocked(liveApi.object).mockReset().mockResolvedValue(LIVE_ZONE);
      const tabs = renderInTabs(makeObj({ simulation: FOLLOW }), "sim");
      await screen.findByText(/Zone temp now reads 21.4/);
      tabs.show("other");
      const calls = vi.mocked(liveApi.object).mock.calls.length;
      await advance(20_000);
      expect(vi.mocked(liveApi.object).mock.calls.length).toBe(calls);
    });

    it("keeps counting a stale followed value's age between polls", async () => {
      vi.mocked(liveApi.object)
        .mockReset()
        .mockResolvedValueOnce(LIVE_ZONE)
        .mockRejectedValue(new ApiError("didn't answer in time", 504));
      renderInTabs(makeObj({ simulation: FOLLOW }), "sim");
      await screen.findByText(/Zone temp now reads 21.4/);
      await advance(5000);
      expect(await screen.findByText(/5 s ago \(stale\)/)).toBeInTheDocument();
      await advance(2000);
      expect(screen.getByText(/7 s ago \(stale\)/)).toBeInTheDocument();
    });
  });

  it("draws a multi-state cycle with the object's state names", async () => {
    const user = userEvent.setup();
    renderTab(makeObj({ object_type: "multi-state-value", state_text: ["Off", "Low", "High"] }));
    await user.click(screen.getByLabelText("Simulate this object"));
    const preview = screen.getByRole("region", { name: /Preview/ });
    for (const name of ["Off", "Low", "High"]) expect(within(preview).getByText(name)).toBeInTheDocument();
    expect(within(screen.getByRole("group", { name: "States" })).getAllByRole("checkbox")).toHaveLength(3);
  });

  it("turns a simulator off by sending null", async () => {
    const user = userEvent.setup();
    renderTab(makeObj({ simulation: { kind: "noise", center: 1, amplitude: 1, interval_s: 5, decimals: 2 } }));
    await user.click(screen.getByLabelText("Simulate this object"));
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(objectsApi.update).toHaveBeenCalledWith(599002, 5, { simulation: null });
  });

  it("fills in defaults an imported simulator was stored without", async () => {
    const user = userEvent.setup();
    renderTab(makeObj({ simulation: { kind: "sine", center: 1, amplitude: 1, period_s: 10 } }));
    expect(screen.getByLabelText("Update every (s)")).toHaveValue(5);
    expect(screen.getByLabelText("Decimals")).toHaveValue(2);
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(objectsApi.update).toHaveBeenCalledWith(599002, 5, {
      simulation: { kind: "sine", center: 1, amplitude: 1, period_s: 10, interval_s: 5, decimals: 2 },
    });
  });

  it("is disabled on an object a connector feeds", () => {
    renderTab(makeObj({ fed_by_connector_id: 3, fed_by_connector_name: "Weather" }));
    expect(screen.getByText(/Fed by connector Weather/)).toBeInTheDocument();
    expect(screen.getByLabelText("Simulate this object")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("puts the save result under Save", async () => {
    const user = userEvent.setup();
    vi.mocked(objectsApi.update).mockRejectedValueOnce(new Error("simulation: period must be positive"));
    renderTab(makeObj());
    await user.click(screen.getByLabelText("Simulate this object"));
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't save: simulation: period must be positive");
    vi.mocked(objectsApi.update).mockResolvedValueOnce({} as never);
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Saved.");
  });

  it("names every control", async () => {
    const user = userEvent.setup();
    const { container } = renderTab(makeObj());
    await user.click(screen.getByLabelText("Simulate this object"));
    expectNamedControls(container);
  });

  it("keeps an unsaved edit when another tab's save refreshes the object", async () => {
    const user = userEvent.setup();
    const client = new QueryClient();
    const tab = (o: BacnetObjectDTO) => (
      <QueryClientProvider client={client}>
        <SimulationTab deviceId={599002} obj={o} />
      </QueryClientProvider>
    );
    const { rerender } = render(tab(makeObj()));
    await user.click(screen.getByLabelText("Simulate this object"));
    rerender(tab(makeObj({ name: "AV-renamed" })));
    expect(screen.getByLabelText("Simulate this object")).toBeChecked();
  });
});
