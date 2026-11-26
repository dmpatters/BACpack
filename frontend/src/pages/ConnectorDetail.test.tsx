// Two independent test blocks:
//
// - JsonPathPicker: the connector-mapping JSON path picker dropdown,
//   covering its own pure rendering logic with no router/query-client/API
//   mocking, matching this codebase's existing pattern for small shared pieces
//   (e.g. LogTable.test.tsx).
// - ConnectorDetail: a page-level integration test block for the page
//   itself, with a MemoryRouter, a QueryClientProvider, and a vi.mock of
//   the whole endpoints module -- most of the page's behavior remains
//   covered at the backend/API level (tests/web/test_connectors.py)
//   plus manual verification, so this block is intentionally narrow (see
//   each test's own comment for what it targets), not a full page suite.
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, RouterProvider, Routes, createMemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi, beforeEach } from "vitest";
import ConnectorDetail, { JsonPathPicker } from "./ConnectorDetail";
import { LeaveGuard, UnsavedChangesProvider } from "../components/UnsavedChanges";
import { expectNamedControls } from "../testing/controls";
import { connectorsApi, fetchAllObjectsWithDevice, liveApi } from "../api/endpoints";
import type { ConnectorMappingDTO, ConnectorPreviewDTO, ConnectorPreviewField, ConnectorStatusDTO } from "../api/types";

vi.mock("../api/endpoints", () => ({
  connectorsApi: {
    get: vi.fn(),
    update: vi.fn(),
    remove: vi.fn(),
    status: vi.fn(),
    preview: vi.fn(),
    mappings: {
      list: vi.fn(),
      create: vi.fn(),
      update: vi.fn(),
      remove: vi.fn(),
    },
  },
  fetchAllObjectsWithDevice: vi.fn(),
  liveApi: { device: vi.fn().mockResolvedValue({ read_at: "2026-10-16T10:48:02+00:00", objects: [] }) },
}));

const LIVE_ENTRY = {
  object_id: 1, object_type: "analog-input", instance: 1, present_value: 21.4, priority_array: null,
  controlling_priority: null, relinquish_default: null, status_flags: [], event_state: "normal",
  reliability: null, out_of_service: false,
};

describe("JsonPathPicker", () => {
  it("renders nothing when there are no preview fields", () => {
    const { container } = render(<JsonPathPicker fields={[]} onPick={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("lists each field's path and value as an option", () => {
    const fields: ConnectorPreviewField[] = [
      { path: "data.temp", value: 72.5, is_array: false },
      { path: "data.humidity", value: 41, is_array: false },
    ];
    render(<JsonPathPicker fields={fields} onPick={() => {}} />);
    expect(screen.getByRole("option", { name: "data.temp: 72.5" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "data.humidity: 41" })).toBeInTheDocument();
  });

  it("disables an array field instead of listing its value", () => {
    const fields: ConnectorPreviewField[] = [{ path: "readings", value: null, is_array: true }];
    render(<JsonPathPicker fields={fields} onPick={() => {}} />);
    const option = screen.getByRole("option", { name: "readings (an array, not selectable)" });
    expect(option).toBeDisabled();
  });

  it("calls onPick with the chosen field's path", async () => {
    const user = userEvent.setup();
    const onPick = vi.fn();
    const fields: ConnectorPreviewField[] = [{ path: "data.temp", value: 72.5, is_array: false }];
    render(<JsonPathPicker fields={fields} onPick={onPick} />);

    await user.selectOptions(screen.getByRole("combobox"), "data.temp");

    expect(onPick).toHaveBeenCalledWith("data.temp");
  });
});

// Default preview response for renderPage() callers that don't care about
// its exact contents (e.g. the second ConnectorDetail test below) -- keeps
// every test's mocks self-contained the way ObjectDetail.test.tsx's
// renderFor() re-establishes all of its mocks on every call, so no test
// depends on another test having run first and left connectorsApi.preview's
// mockResolvedValue behind (vi.clearAllMocks() in beforeEach only clears
// call history, not mock implementations).
const defaultPreviewResponse: ConnectorPreviewDTO = {
  ok: true,
  response_format: "json",
  fields: [],
};

function renderPage(
  previewResponse: ConnectorPreviewDTO = defaultPreviewResponse,
  overrides: {
    objects?: unknown[];
    mappings?: ConnectorMappingDTO[];
    status?: Record<string, ConnectorStatusDTO>;
    verifyTls?: boolean;
    authType?: string;
    headerNames?: string[];
    missing?: string[];
    unreadable?: string[];
  } = {}
) {
  vi.mocked(connectorsApi.preview).mockResolvedValue(previewResponse);
  vi.mocked(connectorsApi.get).mockResolvedValue({
    id: 42,
    name: "Test Connector",
    url: "http://example.com/api",
    enabled: true,
    poll_interval_seconds: 60,
    timeout_seconds: 10,
    response_format: "json",
    auth_type: overrides.authType ?? "none",
    auth_username: null,
    header_names: overrides.headerNames ?? [],
    missing_credentials: overrides.missing ?? [],
    unreadable_credentials: overrides.unreadable ?? [],
    verify_tls: overrides.verifyTls ?? true,
  } as never);
  vi.mocked(connectorsApi.mappings.list).mockResolvedValue(overrides.mappings ?? []);
  vi.mocked(connectorsApi.status).mockResolvedValue(overrides.status ?? {});
  // At least one unmapped object so `availableObjects` is non-empty and
  // AddMappingForm (the only place JsonPathPicker renders while there are
  // zero existing mappings) actually mounts -- an empty list here makes the
  // "already has a mapping" placeholder render instead, and the picker
  // never appears at all.
  vi.mocked(fetchAllObjectsWithDevice).mockResolvedValue((overrides.objects ?? [
    {
      id: 1,
      deviceName: "Weather Station",
      name: "Outside Air Temp",
      object_type: "analog-input",
      instance_number: 1,
    },
  ]) as never);

  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/connectors/42"]}>
        <Routes>
          <Route path="/connectors/:connectorId" element={<ConnectorDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

// The Add mapping panel: its opener, then its form (the submit button shares the opener's name).
async function openAdd(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Add mapping" }));
  return screen.getByRole("form", { name: "Add mapping" });
}

describe("ConnectorDetail", () => {
  beforeEach(() => vi.clearAllMocks());

  it("populates the JSON path picker without Live preview being enabled", async () => {
    const user = userEvent.setup();
    renderPage({
      ok: true,
      response_format: "json",
      fields: [
        { path: "current.temperature_2m", value: 71.2, is_array: false },
        { path: "current.relative_humidity_2m", value: 48, is_array: false },
      ],
    });

    // Not gated on the checkbox: the picker's options are present while
    // "Live preview" is still unchecked. Matches JsonPathPicker's own
    // "path: value" option-text format (see the JsonPathPicker tests
    // above), not just the bare path, since that's the full text node
    // testing-library actually renders.
    await openAdd(user);
    expect(await screen.findByText("current.temperature_2m: 71.2")).toBeInTheDocument();
    expect(screen.getByLabelText(/live preview/i)).not.toBeChecked();
    expect(vi.mocked(connectorsApi.preview)).toHaveBeenCalledTimes(1);
  });

  it("no longer claims the checkbox is what populates the picker", async () => {
    renderPage();
    // `connector` comes from useQuery and resolves asynchronously even with
    // a mocked, already-resolved promise -- the component returns null
    // until then, so this must await the same way the test above does with
    // findByText, rather than asserting synchronously right after render.
    expect(await screen.findByLabelText(/live preview/i)).toBeInTheDocument();
    expect(screen.queryByText(/populates the JSON path picker/i)).not.toBeInTheDocument();
  });
  const mapping: ConnectorMappingDTO = {
    id: 7, connector_id: 42, object_id: 1, json_path: "data.t", scale: 1, offset: 0, value_map: null,
    remote_object_type: null, remote_object_instance: null, remote_property: null, remote_array_index: null,
    mode: "poll", direction: "read",
  };
  const ai = { id: 1, deviceName: "WS", name: "OAT", object_type: "analog-input", instance_number: 1 };

  // The add form offers only objects the API accepts.
  it("leaves out schedules and greys out objects another connector feeds", async () => {
    const user = userEvent.setup();
    renderPage(undefined, {
      objects: [
        ai,
        { id: 2, deviceName: "WS", name: "SCH", object_type: "schedule", instance_number: 1 },
        { id: 3, deviceName: "WS", name: "RH", object_type: "analog-value", instance_number: 2, fed_by_connector_id: 9, fed_by_connector_name: "Other" },
      ],
    });
    await openAdd(user);
    const select = screen.getByLabelText("Object");
    expect(within(select).queryByText(/SCH/)).not.toBeInTheDocument();
    expect(within(select).getByRole("option", { name: /RH .*\(fed by Other\)/ })).toBeDisabled();
    expect(select).toHaveValue("1");
  });

  it("says so when every object left is fed elsewhere", async () => {
    renderPage(undefined, {
      objects: [{ ...ai, fed_by_connector_id: 9, fed_by_connector_name: "Other" }],
    });
    expect(await screen.findByText(/already has a mapping/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add mapping" })).not.toBeInTheDocument();
  });

  it("keeps what you entered when adding a mapping fails", async () => {
    vi.mocked(connectorsApi.mappings.create).mockRejectedValue(new Error("object 1 is already fed"));
    renderPage();
    const user = userEvent.setup();
    const form = await openAdd(user);
    await user.type(within(form).getByLabelText("JSON path"), "data.t");
    await user.click(within(form).getByRole("button", { name: "Add mapping" }));
    expect(await screen.findByText("object 1 is already fed")).toBeInTheDocument();
    expect(screen.getByLabelText("JSON path")).toHaveValue("data.t");
  });

  it("shows each mapping's read error", async () => {
    renderPage(undefined, {
      mappings: [mapping],
      status: {
        "42": { last_poll_at: "2026-10-27T12:00:00+00:00",last_success: true, last_error: null, consecutive_failures: 0, read_errors: { "7": "path data.t not found" } },
      },
    });
    expect(await screen.findByText("path data.t not found")).toBeInTheDocument();
  });

  it("shows each mapping's last value from its object's device", async () => {
    vi.mocked(liveApi.device).mockResolvedValue({
      read_at: "2026-10-16T10:48:02+00:00",
      objects: [{ ...LIVE_ENTRY, object_id: 1, priority_array: [...Array(9).fill(null), 21.4, ...Array(6).fill(null)] }],
    });
    renderPage(undefined, {
      mappings: [mapping],
      objects: [{ ...ai, device_id: 599003, units: "degreesFahrenheit" }],
    });
    const table = await screen.findByRole("table", { name: "Mappings" });
    expect(within(table).getByRole("columnheader", { name: "Last value" })).toBeInTheDocument();
    expect(await within(table).findByText("21.4 °F")).toHaveClass("bp-cell-mono");
    expect(liveApi.device).toHaveBeenCalledWith(599003);
  });

  it("shows an em dash when its device has no live value for the object", async () => {
    vi.mocked(liveApi.device).mockResolvedValue({ read_at: "2026-10-16T10:48:02+00:00", objects: [] });
    renderPage(undefined, { mappings: [mapping], objects: [{ ...ai, device_id: 599003 }] });
    const table = await screen.findByRole("table", { name: "Mappings" });
    const headers = within(table).getAllByRole("columnheader").map((h) => h.textContent);
    const row = within(table).getAllByRole("row")[1];
    expect(within(row).getAllByRole("cell")[headers.indexOf("Last value")]).toHaveTextContent("—");
    expect(headers).toContain("Last value");
  });

  it("shows mappings read-only, and edits one in a form under the table", async () => {
    vi.mocked(connectorsApi.mappings.update).mockResolvedValue(mapping as never);
    renderPage(undefined, { mappings: [mapping] });
    const user = userEvent.setup();
    const table = await screen.findByRole("table", { name: "Mappings" });
    expect(within(table).queryByRole("textbox")).not.toBeInTheDocument();
    expect(within(table).queryByRole("spinbutton")).not.toBeInTheDocument();
    await user.click(within(table).getByRole("button", { name: "Edit" }));
    const editedRow = within(table).getAllByRole("row")[1];
    expect(editedRow).toHaveAttribute("data-selected");
    expect(editedRow).not.toHaveAttribute("aria-selected");
    const form = screen.getByRole("form", { name: /Edit mapping/ });
    await user.clear(within(form).getByLabelText("JSON path"));
    await user.type(within(form).getByLabelText("JSON path"), "data.humidity");
    await user.click(within(form).getByRole("button", { name: "Save mapping" }));
    expect(connectorsApi.mappings.update).toHaveBeenCalledWith(42, 7, { json_path: "data.humidity", scale: 1, offset: 0 });
  });

  it("won't save a mapping with its scale cleared", async () => {
    renderPage(undefined, { mappings: [mapping] });
    const user = userEvent.setup();
    await user.click(within(await screen.findByRole("table", { name: "Mappings" })).getByRole("button", { name: "Edit" }));
    const form = screen.getByRole("form", { name: /Edit mapping/ });
    const scale = within(form).getByLabelText("Scale");
    await user.tripleClick(scale);
    await user.keyboard("{Backspace}");
    await user.click(within(form).getByRole("button", { name: "Save mapping" }));
    expect(connectorsApi.mappings.update).not.toHaveBeenCalled();
    expect(within(form).getByRole("alert")).toHaveTextContent(/scale and offset/i);
  });

  it("asks before deleting a mapping", async () => {
    vi.mocked(connectorsApi.mappings.remove).mockResolvedValue(undefined as never);
    renderPage(undefined, { mappings: [mapping] });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Delete" }));
    expect(screen.getByRole("alertdialog", { name: /Delete the mapping for/ })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(connectorsApi.mappings.remove).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Delete" }));
    await user.click(screen.getByRole("button", { name: "Delete mapping" }));
    expect(connectorsApi.mappings.remove).toHaveBeenCalledWith(42, 7);
  });

  const sixteen = Array.from({ length: 16 }, (_, i) => ({ ...mapping, id: 100 + i, json_path: `data.p${i + 1}` }));
  const bodyRows = (table: HTMLElement) => within(table).getAllByRole("row").length - 1;

  it("searches the mappings and says when none match", async () => {
    renderPage(undefined, { mappings: sixteen });
    const user = userEvent.setup();
    const table = await screen.findByRole("table", { name: "Mappings" });
    await user.type(screen.getByLabelText("Search mappings"), "p16");
    expect(bodyRows(table)).toBe(1);
    expect(screen.getByText("1 of 16 mappings")).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Search mappings"));
    await user.type(screen.getByLabelText("Search mappings"), "zzz");
    expect(screen.queryByRole("table", { name: "Mappings" })).not.toBeInTheDocument();
    expect(screen.getByText("No mappings match.")).toBeInTheDocument();
  });

  it("stops filtering once the search box goes away", async () => {
    vi.mocked(connectorsApi.mappings.remove).mockResolvedValue(undefined as never);
    renderPage(undefined, { mappings: sixteen });
    const user = userEvent.setup();
    const table = await screen.findByRole("table", { name: "Mappings" });
    await user.type(screen.getByLabelText("Search mappings"), "p1");
    expect(bodyRows(table)).toBe(8);
    vi.mocked(connectorsApi.mappings.list).mockResolvedValue(sixteen.slice(0, 15));
    await user.click(within(table).getAllByRole("button", { name: "Delete" })[7]);
    await user.click(screen.getByRole("button", { name: "Delete mapping" }));
    expect(await screen.findByRole("heading", { name: "Mappings (15)" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Search mappings")).not.toBeInTheDocument();
    expect(bodyRows(screen.getByRole("table", { name: "Mappings" }))).toBe(15);
  });

  it("keeps a half-typed add form through an edit", async () => {
    const free = { id: 2, deviceName: "WS", name: "RH", object_type: "analog-input", instance_number: 2 };
    renderPage(undefined, { mappings: [mapping], objects: [ai, free] });
    const user = userEvent.setup();
    const add = await openAdd(user);
    await user.type(within(add).getByLabelText("JSON path"), "data.x");
    await user.click(within(await screen.findByRole("table", { name: "Mappings" })).getByRole("button", { name: "Edit" }));
    expect(screen.getByRole("dialog", { name: /^Edit mapping .*OAT/ })).toBeVisible();
    expect(screen.queryByRole("dialog", { name: "Add mapping" })).not.toBeInTheDocument();
    const form = screen.getByRole("form", { name: /Edit mapping/ });
    await user.click(within(form).getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: "Add mapping" }));
    expect(within(screen.getByRole("form", { name: "Add mapping" })).getByLabelText("JSON path")).toHaveValue("data.x");
  });

  it("saves an edit and closes the panel, but keeps the add panel open after an add", async () => {
    vi.mocked(connectorsApi.mappings.update).mockResolvedValue(mapping as never);
    vi.mocked(connectorsApi.mappings.create).mockResolvedValue(mapping as never);
    const free = { id: 2, deviceName: "WS", name: "RH", object_type: "analog-input", instance_number: 2 };
    renderPage(undefined, { mappings: [mapping], objects: [ai, free] });
    const user = userEvent.setup();
    const add = await openAdd(user);
    await user.click(within(add).getByRole("button", { name: "Add mapping" }));
    expect(await screen.findByText("Mapping added.")).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Add mapping" })).toBeVisible();

    await user.click(within(await screen.findByRole("table", { name: "Mappings" })).getByRole("button", { name: "Edit" }));
    await user.click(within(screen.getByRole("form", { name: /Edit mapping/ })).getByRole("button", { name: "Save mapping" }));
    await vi.waitFor(() => expect(screen.queryByRole("dialog", { name: /Edit mapping/ })).not.toBeInTheDocument());
  });

  it("keeps a half-typed edit when its panel is closed with Escape, and reopens it with the same row's Edit", async () => {
    renderPage(undefined, { mappings: [mapping] });
    const user = userEvent.setup();
    const table = await screen.findByRole("table", { name: "Mappings" });
    await user.click(within(table).getByRole("button", { name: "Edit" }));
    const path = within(screen.getByRole("form", { name: /Edit mapping/ })).getByLabelText("JSON path");
    await user.type(path, ".more");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: /Edit mapping/ })).not.toBeInTheDocument();
    await user.click(within(table).getByRole("button", { name: "Edit" }));
    expect(within(screen.getByRole("form", { name: /Edit mapping/ })).getByLabelText("JSON path")).toHaveValue("data.t.more");
  });

  it("puts the Add mapping button above the mappings table", async () => {
    renderPage(undefined, { mappings: [mapping], objects: [ai, { id: 2, deviceName: "WS", name: "RH", object_type: "analog-input", instance_number: 2 }] });
    const table = await screen.findByRole("table", { name: "Mappings" });
    const add = screen.getByRole("button", { name: "Add mapping" });
    expect(add.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("says so inside the panel once the last free object has a mapping", async () => {
    vi.mocked(connectorsApi.mappings.create).mockResolvedValue(mapping as never);
    renderPage(undefined, { objects: [ai] });
    const user = userEvent.setup();
    const form = await openAdd(user);
    vi.mocked(fetchAllObjectsWithDevice).mockResolvedValue([{ ...ai, fed_by_connector_id: 42, fed_by_connector_name: "Test Connector" }] as never);
    await user.click(within(form).getByRole("button", { name: "Add mapping" }));
    expect(await within(screen.getByRole("dialog", { name: "Add mapping" })).findByText(/already has a mapping/)).toBeInTheDocument();
    expect(screen.getAllByText(/already has a mapping/)).toHaveLength(1);

    await user.click(within(screen.getByRole("dialog", { name: "Add mapping" })).getByRole("button", { name: "Close" }));
    // The closed panel stays mounted, hidden.
    const shown = screen.getAllByText(/already has a mapping/).filter((el) => el.closest("[hidden]") === null);
    expect(shown).toHaveLength(1);
  });

  it("leaves the table beside the panel usable", async () => {
    renderPage(undefined, { mappings: [mapping], objects: [ai, { id: 2, deviceName: "WS", name: "RH", object_type: "analog-input", instance_number: 2 }] });
    const user = userEvent.setup();
    await openAdd(user);
    await user.click(within(await screen.findByRole("table", { name: "Mappings" })).getByRole("button", { name: "Delete" }));
    expect(screen.getByRole("alertdialog", { name: /Delete the mapping for/ })).toBeInTheDocument();
  });

  it("shows a read error as an error pill with the message clipped beside it", async () => {
    renderPage(undefined, {
      mappings: [mapping],
      status: { "42": { last_poll_at: "2026-10-27T12:00:00+00:00",last_success: true, last_error: null, consecutive_failures: 0, read_errors: { "7": "path data.t not found" } } },
    });
    expect(await screen.findByText("error")).toHaveClass("bp-pill--danger");
    expect(screen.getByText("path data.t not found")).toHaveClass("bp-cell-clip");
  });

  it("loads verify_tls into the settings form and saves it back", async () => {
    vi.mocked(connectorsApi.update).mockResolvedValue({} as never);
    renderPage(undefined, { verifyTls: false });
    const user = userEvent.setup();
    const verify = await screen.findByLabelText("Verify TLS certificate");
    expect(verify).not.toBeChecked();
    expect(screen.getByText(/self-signed endpoints/)).toBeInTheDocument();

    await user.click(verify);
    expect(screen.queryByText(/self-signed endpoints/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(connectorsApi.update).toHaveBeenCalledWith(42, expect.objectContaining({ verify_tls: true }));
  });

  it("keeps text typed after Save when the connector refetch lands, and rebases an untouched field", async () => {
    vi.mocked(connectorsApi.update).mockResolvedValue({} as never);
    renderPage();
    const user = userEvent.setup();
    const name = await screen.findByLabelText("Name");
    await user.clear(name);
    await user.type(name, "Renamed");

    // The refetch after Save brings the saved name and a timeout changed elsewhere.
    const refetched = {
      id: 42, name: "Renamed", url: "http://example.com/api", enabled: true, poll_interval_seconds: 60,
      timeout_seconds: 20, response_format: "json", auth_type: "none", auth_username: null, header_names: [],
      missing_credentials: [], unreadable_credentials: [], verify_tls: true,
    } as never;
    let land: (value: never) => void = () => undefined;
    // Every later get answers with the changed connector; the first is held in flight.
    vi.mocked(connectorsApi.get).mockResolvedValue(refetched);
    vi.mocked(connectorsApi.get).mockImplementationOnce(() => new Promise((resolve) => (land = resolve as typeof land)));
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(connectorsApi.get).toHaveBeenCalledTimes(2));
    await user.clear(screen.getByLabelText("Poll interval (s)"));
    await user.type(screen.getByLabelText("Poll interval (s)"), "45");

    await act(async () => land(refetched));
    // TanStack hands the refetched data over on a later tick than act flushes.
    await waitFor(() => expect(screen.getByLabelText("Timeout (s)")).toHaveValue(20));
    expect(screen.getByLabelText("Poll interval (s)")).toHaveValue(45);
    expect(screen.getByLabelText("Name")).toHaveValue("Renamed");
  });

  it("keeps a token typed while the save is in flight, and clears the one that was sent", async () => {
    let finish: (value: never) => void = () => undefined;
    vi.mocked(connectorsApi.update).mockImplementationOnce(() => new Promise((resolve) => (finish = resolve as typeof finish)));
    renderPage(undefined, { authType: "bearer" });
    const user = userEvent.setup();
    const token = await screen.findByLabelText("Token");
    await user.type(token, "abc");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(connectorsApi.update).toHaveBeenCalled());
    expect(vi.mocked(connectorsApi.update).mock.calls[0][1]).toMatchObject({ auth_token: "abc" });

    await user.type(token, "d");
    await act(async () => finish({} as never));
    expect(screen.getByLabelText("Token")).toHaveValue("abcd");
  });

  it("clears the token once the save that sent it succeeds", async () => {
    vi.mocked(connectorsApi.update).mockResolvedValue({} as never);
    renderPage(undefined, { authType: "bearer" });
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Token"), "abc");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.getByLabelText("Token")).toHaveValue(""));
  });

  it("won't add a mapping with its scale cleared", async () => {
    renderPage();
    const user = userEvent.setup();
    const form = await openAdd(user);
    await user.clear(within(form).getByLabelText("Scale"));
    await user.click(within(form).getByRole("button", { name: "Add mapping" }));
    expect(connectorsApi.mappings.create).not.toHaveBeenCalled();
  });

  it("says it's waiting, not failing, before the first poll", async () => {
    renderPage(undefined, {
      status: { "42": { last_poll_at: null, last_success: null, last_error: null, consecutive_failures: 0 } },
    });
    expect(await screen.findByText("waiting…")).toBeInTheDocument();
    expect(screen.queryByText(/failing/)).not.toBeInTheDocument();
  });

  it("doesn't say 'no errors' while a mapping has a read error", async () => {
    renderPage(undefined, {
      mappings: [mapping],
      status: {
        "42": { last_poll_at: "2026-10-27T12:00:00+00:00",last_success: true, last_error: null, consecutive_failures: 0, read_errors: { "7": "path data.t not found" } },
      },
    });
    expect(await screen.findByText(/1 mapping has a read error/)).toBeInTheDocument();
    expect(screen.queryByText(/no errors/)).not.toBeInTheDocument();
  });

  it("refreshes the JSON path preview after the settings are saved", async () => {
    vi.mocked(connectorsApi.update).mockResolvedValue({} as never);
    renderPage();
    const user = userEvent.setup();
    await screen.findByLabelText("Verify TLS certificate");
    expect(connectorsApi.preview).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "Save" }));
    await vi.waitFor(() => expect(connectorsApi.preview).toHaveBeenCalledTimes(2));
  });

  it("wraps the URL in the live preview banner", async () => {
    renderPage();
    const user = userEvent.setup();
    await user.click(await screen.findByLabelText(/live preview/i));
    const banner = screen.getByText(/every 30s while this page is open/).closest(".bp-callout")!;
    expect(within(banner as HTMLElement).getByText("http://example.com/api")).toHaveClass("url");
  });

  it("asks in the dialog before deleting the connector", async () => {
    vi.mocked(connectorsApi.remove).mockResolvedValue(undefined as never);
    const confirm = vi.spyOn(window, "confirm");
    renderPage(undefined, { mappings: [mapping] });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Delete connector" }));
    const dialog = screen.getByRole("alertdialog", { name: "Delete Test Connector?" });
    expect(dialog).toHaveTextContent("its 1 mapping");
    await user.click(within(dialog).getByRole("button", { name: "Delete connector" }));
    expect(connectorsApi.remove).toHaveBeenCalledWith(42);
    expect(confirm).not.toHaveBeenCalled();
    confirm.mockRestore();
  });

  describe("a URL on another host", () => {
    const WARNING = /clears the saved password, token and header values/;

    async function edit(url: string, overrides: Parameters<typeof renderPage>[1]) {
      const user = userEvent.setup();
      renderPage(undefined, overrides);
      const settings = await screen.findByRole("form", { name: "Settings" });
      const field = within(settings).getByLabelText("URL");
      await user.clear(field);
      await user.type(field, url);
      return settings;
    }

    it("warns before saving when credentials are saved", async () => {
      await edit("http://other.example.com/api", { authType: "basic" });
      expect(screen.getByText(WARNING)).toBeInTheDocument();
    });

    it("warns for a saved header value alone, and for a port change", async () => {
      await edit("http://example.com:8080/api", { headerNames: ["X-Api-Key"] });
      expect(screen.getByText(WARNING)).toBeInTheDocument();
    });

    it("changes the blank-keeps help to blank-leaves-unset while it shows", async () => {
      const settings = await edit("http://other.example.com/api", { authType: "basic", headerNames: ["Accept"] });
      expect(within(settings).getByLabelText("Password")).toHaveAccessibleDescription("Blank leaves the password unset.");
      expect(within(settings).getByLabelText("Header 1 value")).toHaveAccessibleDescription(
        "Blank leaves the value unset."
      );
    });

    it("keeps the blank-keeps help for a URL that keeps them", async () => {
      const settings = await edit("https://example.com/api", { authType: "basic", headerNames: ["Accept"] });
      expect(within(settings).getByLabelText("Password")).toHaveAccessibleDescription("Blank keeps the saved password.");
      expect(within(settings).getByLabelText("Header 1 value")).toHaveAccessibleDescription("Blank keeps the saved value.");
    });

    it("says nothing for the same host or an http to https upgrade", async () => {
      await edit("https://example.com/other", { authType: "bearer" });
      expect(screen.queryByText(WARNING)).not.toBeInTheDocument();
    });

    it("says nothing when no credentials are saved", async () => {
      await edit("http://other.example.com/api", {
        authType: "basic",
        headerNames: ["X-Api-Key"],
        missing: ["password", "header X-Api-Key"],
      });
      expect(screen.queryByText(WARNING)).not.toBeInTheDocument();
    });
  });

  it("shows the auth fields only for the saved or chosen auth type, and sends only those", async () => {
    vi.mocked(connectorsApi.update).mockResolvedValue({} as never);
    const user = userEvent.setup();
    renderPage();
    const settings = await screen.findByRole("form", { name: "Settings" });
    expect(within(settings).queryByLabelText("Username")).not.toBeInTheDocument();
    await user.selectOptions(within(settings).getByLabelText("Auth"), "bearer");
    await user.type(within(settings).getByLabelText("Token"), "tok");
    await user.click(within(settings).getByRole("button", { name: "Save" }));
    const body = vi.mocked(connectorsApi.update).mock.calls[0][1];
    expect(body).toMatchObject({ auth_type: "bearer", auth_token: "tok" });
    expect(body).not.toHaveProperty("auth_username");
  });

  it("lists the saved headers, says which have no value, and keeps a value left blank", async () => {
    vi.mocked(connectorsApi.update).mockResolvedValue({} as never);
    const user = userEvent.setup();
    renderPage(undefined, { headerNames: ["X-Api-Key", "Accept"], missing: ["header X-Api-Key"] });
    const settings = await screen.findByRole("form", { name: "Settings" });
    expect(within(settings).getByLabelText("Header 1 name")).toHaveValue("X-Api-Key");
    expect(within(settings).getByLabelText("Header 1 value")).toHaveValue("");
    expect(within(settings).getByLabelText("Header 1 value")).toHaveAccessibleDescription("Not set.");
    expect(within(settings).getByLabelText("Header 2 value")).toHaveAccessibleDescription("Blank keeps the saved value.");
    await user.click(within(settings).getByRole("button", { name: "Save" }));
    expect(vi.mocked(connectorsApi.update).mock.calls[0][1]).toMatchObject({
      headers: { "X-Api-Key": null, Accept: null },
    });
  });

  it("sets a header's value, removes one and adds one", async () => {
    vi.mocked(connectorsApi.update).mockResolvedValue({} as never);
    const user = userEvent.setup();
    renderPage(undefined, { headerNames: ["X-Api-Key", "Accept"], missing: ["header X-Api-Key"] });
    const settings = await screen.findByRole("form", { name: "Settings" });
    await user.type(within(settings).getByLabelText("Header 1 value"), "k");
    await user.click(within(settings).getByRole("button", { name: "Remove header 2" }));
    await user.click(within(settings).getByRole("button", { name: "Add header" }));
    await user.type(within(settings).getByLabelText("Header 2 name"), "X-New");
    await user.type(within(settings).getByLabelText("Header 2 value"), "n");
    await user.click(within(settings).getByRole("button", { name: "Save" }));
    expect(vi.mocked(connectorsApi.update).mock.calls[0][1].headers).toEqual({ "X-Api-Key": "k", "X-New": "n" });
  });

  it("says when the saved password isn't set", async () => {
    renderPage(undefined, { authType: "basic", missing: ["password"] });
    const settings = await screen.findByRole("form", { name: "Settings" });
    expect(within(settings).getByLabelText("Password")).toHaveAccessibleDescription(/^Not set\./);
  });

  it("says when a saved password can't be read and has to be entered again", async () => {
    renderPage(undefined, { authType: "basic", missing: ["password"], unreadable: ["password"] });
    const settings = await screen.findByRole("form", { name: "Settings" });
    expect(within(settings).getByLabelText("Password")).toHaveAccessibleDescription(
      "Saved password can't be read. Enter it again."
    );
  });

  it("says when a saved token can't be read and has to be entered again", async () => {
    renderPage(undefined, { authType: "bearer", missing: ["token"], unreadable: ["token"] });
    const settings = await screen.findByRole("form", { name: "Settings" });
    expect(within(settings).getByLabelText("Token")).toHaveAccessibleDescription(
      "Saved token can't be read. Enter it again."
    );
  });

  it("says when a saved header value can't be read and has to be entered again", async () => {
    renderPage(undefined, {
      headerNames: ["X-Api-Key", "Accept"],
      missing: ["header X-Api-Key"],
      unreadable: ["header X-Api-Key"],
    });
    const settings = await screen.findByRole("form", { name: "Settings" });
    expect(within(settings).getByLabelText("Header 1 value")).toHaveAccessibleDescription(
      "Saved value can't be read. Enter it again."
    );
    expect(within(settings).getByLabelText("Header 2 value")).toHaveAccessibleDescription("Blank keeps the saved value.");
  });

  it("puts a settings save error under Save", async () => {
    vi.mocked(connectorsApi.update).mockRejectedValue(new Error("URL must be http(s)"));
    const user = userEvent.setup();
    renderPage();
    const settings = await screen.findByRole("form", { name: "Settings" });
    await user.click(within(settings).getByRole("button", { name: "Save" }));
    expect(await within(settings).findByRole("alert")).toHaveTextContent("Couldn't save: URL must be http(s)");
  });

  it("names every control, the JSON path picker included", async () => {
    renderPage(
      { ok: true, response_format: "json", fields: [{ path: "data.t", value: 1, is_array: false }] } as ConnectorPreviewDTO,
      {
        mappings: [mapping],
        objects: [
          { id: 1, deviceName: "Weather Station", name: "Outside Air Temp", object_type: "analog-input", instance_number: 1 },
          { id: 2, deviceName: "Weather Station", name: "Humidity", object_type: "analog-input", instance_number: 2 },
        ],
      }
    );
    await screen.findByRole("table", { name: "Mappings" });
    await openAdd(userEvent.setup());
    await screen.findByRole("combobox", { name: "Pick a JSON path from the preview" });
    expectNamedControls(document.body);
  });

  function renderGuarded() {
    renderPage();
    cleanup();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const router = createMemoryRouter(
      [
        {
          path: "*",
          element: (
            <UnsavedChangesProvider>
              <Routes>
                <Route path="/connectors/:connectorId" element={<ConnectorDetail />} />
                <Route path="*" element={<p>connector list</p>} />
              </Routes>
              <LeaveGuard />
            </UnsavedChangesProvider>
          ),
        },
      ],
      { initialEntries: ["/connectors/42"] }
    );
    render(
      <QueryClientProvider client={client}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    );
    return router;
  }

  it("asks before the nav drops an unsaved settings edit", async () => {
    const user = userEvent.setup();
    const router = renderGuarded();
    const settings = await screen.findByRole("form", { name: "Settings" });
    await user.type(within(settings).getByLabelText("Name"), " 2");
    await act(() => router.navigate("/connectors"));
    expect(screen.getByRole("alertdialog", { name: "Discard unsaved changes?" })).toBeInTheDocument();
  });

  it("asks once, in the delete dialog, when deleting with an unsaved settings edit", async () => {
    vi.mocked(connectorsApi.remove).mockResolvedValue(undefined as never);
    const user = userEvent.setup();
    const router = renderGuarded();
    const settings = await screen.findByRole("form", { name: "Settings" });
    await user.type(within(settings).getByLabelText("Name"), " 2");
    await user.click(screen.getByRole("button", { name: "Delete connector" }));
    const dialog = screen.getByRole("alertdialog", { name: "Delete Test Connector?" });
    await user.click(within(dialog).getByRole("button", { name: "Delete connector" }));
    await screen.findByText("connector list");
    expect(screen.queryByRole("alertdialog", { name: "Discard unsaved changes?" })).not.toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/connectors");
  });
});
