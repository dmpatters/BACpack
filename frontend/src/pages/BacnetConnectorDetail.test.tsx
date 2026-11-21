import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import BacnetConnectorDetail, { formatPoint, formatValueMap, parseValueMap } from "./BacnetConnectorDetail";
import { connectorsApi, liveApi, objectsApi } from "../api/endpoints";
import type { ConnectorDTO } from "../api/types";
import { expectNamedControls } from "../testing/controls";

vi.mock("../api/endpoints", () => ({
  connectorsApi: {
    update: vi.fn(),
    remove: vi.fn(),
    status: vi.fn(),
    mappings: { list: vi.fn(), create: vi.fn(), update: vi.fn(), remove: vi.fn() },
    writeMappings: { list: vi.fn(), create: vi.fn(), update: vi.fn(), remove: vi.fn() },
  },
  objectsApi: { list: vi.fn() },
  clientToolApi: { objectList: vi.fn() },
  liveApi: { device: vi.fn().mockResolvedValue({ read_at: "2026-10-16T10:48:02+00:00", objects: [] }) },
}));

const connector = {
  id: 7,
  name: "To AHU",
  type: "bacnet",
  enabled: true,
  url: "",
  poll_interval_seconds: 30,
  timeout_seconds: 10,
  device_id: 599001,
  remote_device_instance: 1234,
  remote_address: null,
  rewrite_interval_seconds: 60,
  mapping_count: 1,
  write_mapping_count: 1,
} as ConnectorDTO;

const defaultReads = [
  {
    id: 11, connector_id: 7, object_id: 1, json_path: null, scale: 1, offset: 0, value_map: null,
    remote_object_type: "analog-input", remote_object_instance: 3, remote_property: "present-value",
    remote_array_index: null, mode: "poll", direction: "read",
  },
];
const defaultStatus = {
  "7": {
    last_poll_at: "2026-09-27T12:00:00+00:00", last_success: true, last_error: null,
    consecutive_failures: 0, bound_address: "10.0.0.7",
    read_errors: { "11": "analog-input:3 present-value: object: unknown-object" },
    write_status: { "21": { error: null, last_sent_at: "2026-09-27T12:00:01+00:00" } },
  },
};

const defaultWrites = [
  {
    id: 21, connector_id: 7, object_id: 2, remote_object_type: "analog-value",
    remote_object_instance: 9, remote_property: "present-value", remote_array_index: null, priority: 9,
  },
];

function renderPage(
  objects?: unknown[],
  reads: unknown[] = defaultReads,
  status: unknown = defaultStatus,
  writes: unknown[] = defaultWrites
) {
  vi.mocked(connectorsApi.mappings.list).mockResolvedValue(reads as never);
  vi.mocked(connectorsApi.writeMappings.list).mockResolvedValue(writes as never);
  vi.mocked(connectorsApi.status).mockResolvedValue(status as never);
  vi.mocked(objectsApi.list).mockResolvedValue(
    (objects ?? [
      { id: 1, device_id: 599001, object_type: "analog-value", instance_number: 1, name: "Zone temp" },
      { id: 2, device_id: 599001, object_type: "analog-value", instance_number: 2, name: "Setpoint" },
    ]) as never
  );
  vi.mocked(connectorsApi.writeMappings.create).mockResolvedValue({} as never);

  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <BacnetConnectorDetail connector={connector} />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

// A kind's panel: its opener (the submit button shares the opener's name), then its form.
async function openAdd(kind: "read" | "write") {
  await userEvent.setup().click(await screen.findByRole("button", { name: `Add ${kind} mapping` }));
  return screen.getByRole("form", { name: `Add ${kind} mapping` });
}

describe("formatPoint", () => {
  it("formats a remote point", () => {
    expect(formatPoint("analog-input", 3, "present-value", null)).toBe("analog-input:3 present-value");
    expect(formatPoint("device", 5, "object-list", 2)).toBe("device:5 object-list[2]");
  });
});

describe("formatValueMap", () => {
  it("is the inverse of parseValueMap", () => {
    expect(formatValueMap({ active: 1, inactive: 0 })).toBe("active=1, inactive=0");
    expect(formatValueMap(null)).toBe("");
  });

  it("quotes a name holding a comma, an equals sign or a quote, so it parses back", () => {
    const map = { "a,b": 1, "x=y": 2, 'say "hi"': 3, " padded ": 4, plain: 5 };
    const text = formatValueMap(map);
    expect(text).toBe('"a,b"=1, "x=y"=2, "say \\"hi\\""=3, " padded "=4, plain=5');
    expect(parseValueMap(text)).toEqual(map);
  });
});

describe("parseValueMap", () => {
  it("parses plain and quoted names, and refuses what it can't parse", () => {
    expect(parseValueMap(" active = 1 ,inactive=0")).toEqual({ active: 1, inactive: 0 });
    expect(parseValueMap('"on, really" = 1')).toEqual({ "on, really": 1 });
    expect(parseValueMap("")).toBeUndefined();
    for (const bad of ["active", "active=on", "a=1,", "a=1=2", '"open=1', '"a" b=1', "=1"]) {
      expect(parseValueMap(bad)).toBeNull();
    }
  });
});

describe("BacnetConnectorDetail", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows each read mapping's last value: the feed slot for a read, the present value for a proxy", async () => {
    vi.mocked(liveApi.device).mockResolvedValue({
      read_at: "2026-10-16T10:48:02+00:00",
      objects: [
        {
          object_id: 1, object_type: "analog-value", instance: 1, present_value: 99,
          priority_array: [...Array(7).fill(null), 99, null, 21.4, ...Array(6).fill(null)],
          controlling_priority: 8, relinquish_default: null, status_flags: ["overridden"],
          event_state: "normal", reliability: null, out_of_service: false,
        },
        {
          object_id: 2, object_type: "analog-value", instance: 2, present_value: 55,
          priority_array: [...Array(9).fill(null), 55, ...Array(6).fill(null)],
          controlling_priority: 10, relinquish_default: null, status_flags: [],
          event_state: "normal", reliability: null, out_of_service: false,
        },
      ],
    });
    renderPage(undefined, [
      defaultReads[0],
      { ...defaultReads[0], id: 12, object_id: 2, remote_object_instance: 4, direction: "proxy" },
    ]);
    const table = await screen.findByRole("table", { name: "Read mappings" });
    expect(within(table).getByRole("columnheader", { name: "Last value" })).toBeInTheDocument();
    expect(await within(table).findByText("21.4")).toHaveClass("bp-cell-mono");
    expect(within(table).getByText("55")).toHaveClass("bp-cell-mono");
    expect(liveApi.device).toHaveBeenCalledWith(599001);
  });

  it("reloads the objects after adding or deleting a mapping, so the grey-out stays current", async () => {
    // fed_by_connector_* comes from the objects query.
    renderPage();
    vi.mocked(connectorsApi.mappings.create).mockResolvedValue({} as never);
    vi.mocked(connectorsApi.mappings.remove).mockResolvedValue(undefined as never);
    const user = userEvent.setup();
    const form = await openAdd("read");
    await vi.waitFor(() => expect(objectsApi.list).toHaveBeenCalledTimes(1));

    await user.click(within(form).getByRole("button", { name: "Add read mapping" }));
    await vi.waitFor(() => expect(objectsApi.list).toHaveBeenCalledTimes(2));

    const reads = screen.getByRole("table", { name: "Read mappings" });
    await user.click(within(reads).getByRole("button", { name: "Delete" }));
    await user.click(screen.getByRole("button", { name: "Delete mapping" }));
    await vi.waitFor(() => expect(objectsApi.list).toHaveBeenCalledTimes(3));
  });

  it("greys out objects another mapping already feeds", async () => {
    renderPage([
      { id: 1, device_id: 599001, object_type: "analog-value", instance_number: 1, name: "Zone temp",
        fed_by_connector_id: 3, fed_by_connector_name: "Weather" },
      { id: 2, device_id: 599001, object_type: "analog-value", instance_number: 2, name: "Setpoint",
        fed_by_connector_id: null, fed_by_connector_name: null },
    ]);
    const form = await openAdd("read");
    const option = await within(form).findByRole("option", { name: /Zone temp.*fed by Weather/ });
    expect(option).toBeDisabled();
    expect(within(form).getByLabelText("Local object")).toHaveValue("2");
  });

  it("edits a read mapping", async () => {
    // The mapping's own (fed) object doesn't block the edit.
    renderPage();
    vi.mocked(connectorsApi.mappings.update).mockResolvedValue({} as never);
    const user = userEvent.setup();
    const reads = await screen.findByRole("table", { name: "Read mappings" });
    await user.click(within(reads).getByRole("button", { name: "Edit" }));
    expect(within(reads).getAllByRole("row")[1]).toHaveAttribute("data-selected");
    expect(within(reads).getAllByRole("row")[1]).not.toHaveAttribute("aria-selected");

    const form = screen.getByRole("form", { name: "Edit read mapping" });
    expect(within(form).getByLabelText("Local object")).toBeDisabled();
    expect(within(form).getByLabelText("Remote instance")).toHaveValue(3);
    await user.clear(within(form).getByLabelText("Scale"));
    await user.type(within(form).getByLabelText("Scale"), "2");
    await user.click(within(form).getByRole("button", { name: "Save read mapping" }));

    expect(connectorsApi.mappings.update).toHaveBeenCalledWith(7, 11, {
      remote_object_type: "analog-input",
      remote_object_instance: 3,
      remote_property: "present-value",
      direction: "read",
      scale: 2,
      offset: 0,
      value_map: null,
    });
  });

  it("edits a write mapping and cancels another edit", async () => {
    renderPage();
    vi.mocked(connectorsApi.writeMappings.update).mockResolvedValue({} as never);
    const user = userEvent.setup();
    const writes = await screen.findByRole("table", { name: "Write mappings" });
    await user.click(within(writes).getByRole("button", { name: "Edit" }));

    let form = screen.getByRole("form", { name: "Edit write mapping" });
    await user.click(within(form).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog", { name: "Edit write mapping" })).not.toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Edit write mapping" })).not.toBeInTheDocument();

    await user.click(within(writes).getByRole("button", { name: "Edit" }));
    expect(within(writes).getAllByRole("row")[1]).toHaveAttribute("data-selected");
    form = screen.getByRole("form", { name: "Edit write mapping" });
    await user.clear(within(form).getByLabelText("Priority"));
    await user.type(within(form).getByLabelText("Priority"), "11");
    await user.click(within(form).getByRole("button", { name: "Save write mapping" }));

    expect(connectorsApi.writeMappings.update).toHaveBeenCalledWith(7, 21, {
      remote_object_type: "analog-value",
      remote_object_instance: 9,
      remote_property: "present-value",
      priority: 11,
    });
  });

  it("keeps a half-typed add form through an edit", async () => {
    renderPage();
    const user = userEvent.setup();
    const add = await openAdd("read");
    await user.type(within(add).getByLabelText(/Value map/), "active=1");

    const reads = screen.getByRole("table", { name: "Read mappings" });
    await user.click(within(reads).getByRole("button", { name: "Edit" }));
    expect(screen.queryByRole("form", { name: "Add read mapping" })).not.toBeInTheDocument();
    await user.click(within(screen.getByRole("form", { name: "Edit read mapping" })).getByRole("button", { name: "Cancel" }));

    await user.click(screen.getByRole("button", { name: "Add read mapping" }));
    const back = screen.getByRole("form", { name: "Add read mapping" });
    expect(within(back).getByLabelText(/Value map/)).toHaveValue("active=1");
  });

  it("keeps a half-typed edit when its panel is closed with Escape", async () => {
    renderPage();
    const user = userEvent.setup();
    const reads = await screen.findByRole("table", { name: "Read mappings" });
    await user.click(within(reads).getByRole("button", { name: "Edit" }));
    const scale = within(screen.getByRole("form", { name: "Edit read mapping" })).getByLabelText("Scale");
    await user.clear(scale);
    await user.type(scale, "7");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Edit read mapping" })).not.toBeInTheDocument();
    await user.click(within(reads).getByRole("button", { name: "Edit" }));
    expect(within(screen.getByRole("form", { name: "Edit read mapping" })).getByLabelText("Scale")).toHaveValue(7);
  });

  it("opens each panel by its own name, one at a time", async () => {
    renderPage();
    const user = userEvent.setup();
    await openAdd("read");
    expect(screen.getByRole("dialog", { name: "Add read mapping" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Add write mapping" }));
    expect(screen.getByRole("dialog", { name: "Add write mapping" })).toBeVisible();
    expect(screen.queryByRole("dialog", { name: "Add read mapping" })).not.toBeInTheDocument();
    const reads = screen.getByRole("table", { name: "Read mappings" });
    await user.click(within(reads).getByRole("button", { name: "Edit" }));
    expect(screen.getByRole("dialog", { name: "Edit read mapping" })).toBeVisible();
    expect(screen.queryByRole("dialog", { name: "Add write mapping" })).not.toBeInTheDocument();
  });

  it("leaves the tables beside a panel usable", async () => {
    renderPage();
    const user = userEvent.setup();
    await openAdd("read");
    const reads = screen.getByRole("table", { name: "Read mappings" });
    await user.click(within(reads).getByRole("button", { name: "Delete" }));
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
  });

  it("keeps the add panel open after an add, and closes an edit panel after its save", async () => {
    vi.mocked(connectorsApi.mappings.create).mockResolvedValue({} as never);
    vi.mocked(connectorsApi.mappings.update).mockResolvedValue({} as never);
    renderPage();
    const user = userEvent.setup();
    const add = await openAdd("read");
    await user.click(within(add).getByRole("button", { name: "Add read mapping" }));
    expect(await within(add).findByText("Mapping added.")).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Add read mapping" })).toBeVisible();

    const reads = screen.getByRole("table", { name: "Read mappings" });
    await user.click(within(reads).getByRole("button", { name: "Edit" }));
    await user.click(within(screen.getByRole("form", { name: "Edit read mapping" })).getByRole("button", { name: "Save read mapping" }));
    await vi.waitFor(() => expect(screen.queryByRole("dialog", { name: "Edit read mapping" })).not.toBeInTheDocument());
  });

  it("lists read and write mappings with their status", async () => {
    renderPage();

    const reads = await screen.findByRole("table", { name: "Read mappings" });
    expect(within(reads).getByText("analog-input:3 present-value")).toHaveClass("bp-cell-mono");
    expect(within(reads).getByText("error")).toHaveClass("bp-pill", "bp-pill--danger");
    expect(within(reads).getByText(/unknown-object/)).toHaveClass("bp-cell-clip");
    expect(within(reads).getByText("1")).toHaveClass("bp-cell-num");
    const writes = screen.getByRole("table", { name: "Write mappings" });
    expect(within(writes).getByText("analog-value:9 present-value")).toHaveClass("bp-cell-mono");
    expect(within(writes).getByText("9")).toHaveClass("bp-cell-num");
    expect(within(writes).getByText("ok")).toHaveClass("bp-pill", "bp-pill--ok");
    expect(screen.getByText("10.0.0.7")).toBeInTheDocument();
  });

  it("adds a read mapping with a value map", async () => {
    renderPage();
    vi.mocked(connectorsApi.mappings.create).mockResolvedValue({} as never);
    const user = userEvent.setup();
    const form = await openAdd("read");

    await user.clear(within(form).getByLabelText("Remote object type"));
    await user.type(within(form).getByLabelText("Remote object type"), "binary-input");
    await user.type(within(form).getByLabelText(/Value map/), "active=1, inactive = 0");
    await user.click(within(form).getByRole("button", { name: "Add read mapping" }));

    expect(connectorsApi.mappings.create).toHaveBeenCalledWith(7, {
      object_id: 1,
      remote_object_type: "binary-input",
      remote_object_instance: 1,
      remote_property: "present-value",
      direction: "read",
      scale: 1,
      offset: 0,
      value_map: { active: 1, inactive: 0 },
    });
  });

  it("adds a read mapping without a value map when it's left blank", async () => {
    renderPage();
    vi.mocked(connectorsApi.mappings.create).mockResolvedValue({} as never);
    const user = userEvent.setup();
    const form = await openAdd("read");

    await user.click(within(form).getByRole("button", { name: "Add read mapping" }));

    expect(connectorsApi.mappings.create).toHaveBeenCalledWith(7, {
      object_id: 1,
      remote_object_type: "analog-input",
      remote_object_instance: 1,
      remote_property: "present-value",
      direction: "read",
      scale: 1,
      offset: 0,
    });
  });

  it("refuses a value map it can't parse", async () => {
    renderPage();
    const user = userEvent.setup();
    const form = await openAdd("read");

    await user.type(within(form).getByLabelText(/Value map/), "active=on");
    await user.click(within(form).getByRole("button", { name: "Add read mapping" }));

    expect(within(form).getByRole("alert")).toHaveTextContent(/name=number/);
    expect(connectorsApi.mappings.create).not.toHaveBeenCalled();
  });

  it("adds a write mapping", async () => {
    renderPage();
    const user = userEvent.setup();
    const form = await openAdd("write");

    await user.selectOptions(within(form).getByLabelText("Local object"), "2");
    await user.clear(within(form).getByLabelText("Remote object type"));
    await user.type(within(form).getByLabelText("Remote object type"), "binary-output");
    await user.clear(within(form).getByLabelText("Remote instance"));
    await user.type(within(form).getByLabelText("Remote instance"), "4");
    await user.clear(within(form).getByLabelText("Priority"));
    await user.type(within(form).getByLabelText("Priority"), "12");
    await user.click(within(form).getByRole("button", { name: "Add write mapping" }));

    expect(connectorsApi.writeMappings.create).toHaveBeenCalledWith(7, {
      object_id: 2,
      remote_object_type: "binary-output",
      remote_object_instance: 4,
      remote_property: "present-value",
      priority: 12,
    });
  });
  it("adds a proxy: scaling and the property are fixed, only commandable objects can be picked", async () => {
    renderPage([
      { id: 1, device_id: 599001, object_type: "analog-value", instance_number: 1, name: "Zone temp" },
      { id: 2, device_id: 599001, object_type: "analog-value", instance_number: 2, name: "Setpoint" },
      { id: 3, device_id: 599001, object_type: "binary-input", instance_number: 1, name: "Fan status" },
      { id: 4, device_id: 599001, object_type: "multi-state-value", instance_number: 1, name: "Fan mode" },
    ]);
    vi.mocked(connectorsApi.mappings.create).mockResolvedValue({} as never);
    const user = userEvent.setup();
    const form = await openAdd("read");

    await user.selectOptions(within(form).getByLabelText("Mode"), "proxy");

    expect(within(form).getByLabelText("Scale")).toBeDisabled();
    expect(within(form).getByLabelText("Offset")).toBeDisabled();
    expect(within(form).getByLabelText("Value map")).toBeDisabled();
    expect(within(form).getByLabelText("Remote property")).toBeDisabled();
    expect(within(form).getByRole("option", { name: /Fan status/ })).toBeDisabled();
    expect(within(form).getByRole("option", { name: /Fan mode/ })).not.toBeDisabled();
    await user.click(within(form).getByRole("button", { name: "Add read mapping" }));
    expect(connectorsApi.mappings.create).toHaveBeenCalledWith(
      7,
      expect.objectContaining({ direction: "proxy", scale: 1, offset: 0, remote_property: "present-value" })
    );
  });

  it("shows a proxy's fixed scale, offset and value map, and gives the read ones back", async () => {
    renderPage(undefined, [{ ...defaultReads[0], scale: 2, offset: 3, value_map: { on: 1 } }]);
    const user = userEvent.setup();
    const reads = await screen.findByRole("table", { name: "Read mappings" });
    await user.click(within(reads).getByRole("button", { name: "Edit" }));
    const form = screen.getByRole("form", { name: "Edit read mapping" });

    await user.selectOptions(within(form).getByLabelText("Mode"), "proxy");
    expect(within(form).getByLabelText("Scale")).toHaveValue(1);
    expect(within(form).getByLabelText("Offset")).toHaveValue(0);
    expect(within(form).getByLabelText("Value map")).toHaveValue("");

    await user.selectOptions(within(form).getByLabelText("Mode"), "read");
    expect(within(form).getByLabelText("Scale")).toHaveValue(2);
    expect(within(form).getByLabelText("Offset")).toHaveValue(3);
    expect(within(form).getByLabelText("Value map")).toHaveValue("on=1");
  });

  it("says so, and adds nothing, when no commandable object is free for a proxy", async () => {
    // Not object_id 0 and the API's raw 404.
    renderPage([{ id: 3, device_id: 599001, object_type: "binary-input", instance_number: 1, name: "Fan status" }]);
    const user = userEvent.setup();
    const form = await openAdd("read");

    await user.selectOptions(within(form).getByLabelText("Mode"), "proxy");

    expect(within(form).getByText(/No commandable object is free for a proxy/)).toBeInTheDocument();
    expect(within(form).getByRole("button", { name: "Add read mapping" })).toBeDisabled();
    await user.selectOptions(within(form).getByLabelText("Mode"), "read");
    expect(within(form).getByRole("button", { name: "Add read mapping" })).not.toBeDisabled();
    expect(connectorsApi.mappings.create).not.toHaveBeenCalled();
  });

  it("marks a proxy and shows its last forwarded write", async () => {
    renderPage(
      undefined,
      [
        {
          id: 11, connector_id: 7, object_id: 2, json_path: null, scale: 1, offset: 0, value_map: null,
          remote_object_type: "analog-value", remote_object_instance: 5, remote_property: "present-value",
          remote_array_index: null, mode: "poll", direction: "proxy",
        },
      ],
      {
        "7": {
          last_poll_at: null, last_success: true, last_error: null, consecutive_failures: 0,
          read_errors: { "11": null },
          last_forward: { "11": { at: "2026-10-03T12:00:00+00:00", priority: 8, value: 72, error: null } },
        },
      }
    );

    const table = await screen.findByRole("table", { name: "Read mappings" });
    const row = (await within(table).findByText("analog-value:5 present-value")).closest("tr") as HTMLElement;
    expect(within(row).getByText("proxy")).toHaveClass("bp-pill--info");
    expect(await within(row).findByText(/last write 72 @ 8: ok/)).toHaveClass("bp-cell-clip");
    expect(within(row).getByText("ok")).toHaveClass("bp-pill--ok");
  });

  it("shows a proxy whose read is fine but whose last forward failed as an error", async () => {
    renderPage(
      undefined,
      [
        {
          id: 11, connector_id: 7, object_id: 2, json_path: null, scale: 1, offset: 0, value_map: null,
          remote_object_type: "analog-value", remote_object_instance: 5, remote_property: "present-value",
          remote_array_index: null, mode: "poll", direction: "proxy",
        },
      ],
      {
        "7": {
          last_poll_at: null, last_success: true, last_error: null, consecutive_failures: 0,
          read_errors: { "11": null },
          last_forward: { "11": { at: "2026-10-03T12:00:00+00:00", priority: 8, value: 72, error: "write-access-denied" } },
        },
      }
    );

    const table = await screen.findByRole("table", { name: "Read mappings" });
    const row = (await within(table).findByText("analog-value:5 present-value")).closest("tr") as HTMLElement;
    expect(await within(row).findByText(/last write 72 @ 8: write-access-denied/)).toBeInTheDocument();
    expect(within(row).getByText("error")).toHaveClass("bp-pill--danger");
  });

  // Deleting a write mapping also relinquishes its priority.
  it("asks before deleting a read or write mapping", async () => {
    renderPage();
    const user = userEvent.setup();
    const reads = await screen.findByRole("table", { name: "Read mappings" });
    await user.click(within(reads).getByRole("button", { name: "Delete" }));
    expect(screen.getByRole("alertdialog", { name: /Delete the read mapping/ })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    const writes = screen.getByRole("table", { name: "Write mappings" });
    await user.click(within(writes).getByRole("button", { name: "Delete" }));
    expect(screen.getByRole("alertdialog", { name: /Delete the write mapping/ })).toHaveAccessibleDescription(/relinquish/i);
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(connectorsApi.mappings.remove).not.toHaveBeenCalled();
    expect(connectorsApi.writeMappings.remove).not.toHaveBeenCalled();
  });

  it("won't add a read mapping with its scale cleared", async () => {
    renderPage();
    const user = userEvent.setup();
    const form = await openAdd("read");
    await user.clear(within(form).getByLabelText("Scale"));
    await user.click(within(form).getByRole("button", { name: "Add read mapping" }));
    expect(connectorsApi.mappings.create).not.toHaveBeenCalled();
  });

  it("searches the read and write mappings and says when none match", async () => {
    const reads = Array.from({ length: 16 }, (_, i) => ({ ...defaultReads[0], id: 100 + i, remote_object_instance: i + 1 }));
    const writes = Array.from({ length: 16 }, (_, i) => ({ ...defaultWrites[0], id: 200 + i, remote_object_instance: i + 1 }));
    renderPage(undefined, reads, {}, writes);
    const user = userEvent.setup();
    const readTable = await screen.findByRole("table", { name: "Read mappings" });
    await user.type(screen.getByLabelText("Search read mappings"), "analog-input:16");
    expect(within(readTable).getAllByRole("row")).toHaveLength(2);
    expect(screen.getByText("1 of 16 read mappings")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Search read mappings"), "zzz");
    expect(screen.queryByRole("table", { name: "Read mappings" })).not.toBeInTheDocument();
    expect(screen.getByText("No read mappings match.")).toBeInTheDocument();

    await user.type(screen.getByLabelText("Search write mappings"), "zzz");
    expect(screen.queryByRole("table", { name: "Write mappings" })).not.toBeInTheDocument();
    expect(screen.getByText("No write mappings match.")).toBeInTheDocument();
  });

  it("shows a write's last-sent time once, under a header that names the zone", async () => {
    renderPage();
    const table = await screen.findByRole("table", { name: "Write mappings" });
    expect(within(table).getByRole("columnheader", { name: "Last sent (UTC)" })).toBeInTheDocument();
    expect(within(table).getByText("2026-09-27 12:00:01")).toBeInTheDocument();
  });

  it("won't save the settings with the poll interval cleared", async () => {
    renderPage();
    const user = userEvent.setup();
    await user.clear(await screen.findByLabelText(/poll interval/i));
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(connectorsApi.update).not.toHaveBeenCalled();
  });

  it("asks in the dialog before deleting the connector, counting both kinds of mapping", async () => {
    vi.mocked(connectorsApi.remove).mockResolvedValue(undefined as never);
    renderPage();
    const user = userEvent.setup();
    await screen.findByRole("table", { name: "Read mappings" });
    await screen.findByRole("table", { name: "Write mappings" });
    await user.click(screen.getByRole("button", { name: "Delete connector" }));
    const dialog = screen.getByRole("alertdialog", { name: "Delete To AHU?" });
    expect(dialog).toHaveTextContent("its 2 mappings");
    await user.click(within(dialog).getByRole("button", { name: "Delete connector" }));
    expect(connectorsApi.remove).toHaveBeenCalledWith(7);
  });

  it("keeps text typed after Save when the connector refetch lands, and rebases an untouched field", async () => {
    vi.mocked(connectorsApi.update).mockResolvedValue({} as never);
    vi.mocked(connectorsApi.mappings.list).mockResolvedValue([] as never);
    vi.mocked(connectorsApi.writeMappings.list).mockResolvedValue([] as never);
    vi.mocked(connectorsApi.status).mockResolvedValue({} as never);
    vi.mocked(objectsApi.list).mockResolvedValue([] as never);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const page = (c: ConnectorDTO) => (
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <BacnetConnectorDetail connector={c} />
        </MemoryRouter>
      </QueryClientProvider>
    );
    const view = render(page(connector));
    const user = userEvent.setup();
    const name = await screen.findByLabelText("Name");
    await user.clear(name);
    await user.type(name, "Renamed");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(connectorsApi.update).toHaveBeenCalled());
    await user.clear(screen.getByLabelText(/poll interval/i));
    await user.type(screen.getByLabelText(/poll interval/i), "45");

    // The refetch after Save lands: the saved name, and a timeout another tab changed.
    view.rerender(page({ ...connector, name: "Renamed", timeout_seconds: 20 }));

    expect(screen.getByLabelText(/poll interval/i)).toHaveValue(45);
    expect(screen.getByLabelText("Name")).toHaveValue("Renamed");
    expect(screen.getByLabelText(/timeout/i)).toHaveValue(20);
    expect(screen.queryByText("Saved.")).not.toBeInTheDocument();
  });

  it("puts a settings save error under Save", async () => {
    vi.mocked(connectorsApi.update).mockRejectedValue(new Error("remote_device_instance: out of range"));
    const user = userEvent.setup();
    renderPage();
    const settings = await screen.findByRole("form", { name: "Settings" });
    await user.click(within(settings).getByRole("button", { name: "Save" }));
    expect(await within(settings).findByRole("alert")).toHaveTextContent("Couldn't save: remote_device_instance: out of range");
  });

  it("puts a mapping add error inside the add form", async () => {
    vi.mocked(connectorsApi.mappings.create).mockRejectedValue(new Error("object 1 is already fed"));
    const user = userEvent.setup();
    renderPage();
    const form = await openAdd("read");
    await user.click(within(form).getByRole("button", { name: "Add read mapping" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent("Couldn't add the mapping: object 1 is already fed");
  });

  it("names every control, both add forms included", async () => {
    renderPage();
    await openAdd("read");
    await openAdd("write");
    expectNamedControls(document.body);
  });
});
