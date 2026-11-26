import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ConnectorsList, { connectorCreateBody, describeTarget, type ConnectorForm } from "./ConnectorsList";
import { expectNamedControls } from "../testing/controls";
import { connectorsApi, devicesApi } from "../api/endpoints";
import type { ConnectorDTO } from "../api/types";

vi.mock("../api/endpoints", () => ({
  connectorsApi: { list: vi.fn(), status: vi.fn(), create: vi.fn(), remove: vi.fn() },
  devicesApi: { list: vi.fn() },
}));

const form: ConnectorForm = {
  type: "bacnet",
  name: "Remote",
  url: "http://ignored",
  pollIntervalSeconds: "30",
  timeoutSeconds: "10",
  responseFormat: "json",
  authType: "none",
  authUsername: "",
  authPassword: "",
  authToken: "",
  verifyTls: true,
  deviceId: "599001",
  remoteInstance: "1234",
  remoteAddress: " ",
  rewriteIntervalSeconds: "60",
  headers: [],
};

const httpForm: ConnectorForm = { ...form, type: "http-poll" };

describe("connectorCreateBody", () => {
  it("sends only the bacnet fields for a bacnet connector", () => {
    expect(connectorCreateBody(form)).toEqual({
      name: "Remote",
      type: "bacnet",
      poll_interval_seconds: 30,
      timeout_seconds: 10,
      device_id: 599001,
      remote_device_instance: 1234,
      remote_address: null,
      rewrite_interval_seconds: 60,
    });
  });

  it("sends only the http fields for an http-poll connector", () => {
    const body = connectorCreateBody({ ...form, type: "http-poll" });
    expect(body).toMatchObject({ type: "http-poll", url: "http://ignored", response_format: "json" });
    expect(body).not.toHaveProperty("device_id");
  });

  it("sends verify_tls for an http-poll connector only", () => {
    expect(connectorCreateBody({ ...form, type: "http-poll", verifyTls: false })).toMatchObject({ verify_tls: false });
    expect(connectorCreateBody({ ...form, verifyTls: false })).not.toHaveProperty("verify_tls");
  });
});

describe("describeTarget", () => {
  it("names the remote device and the owning device", () => {
    const connector = { type: "bacnet", url: "", device_id: 599001, remote_device_instance: 1234, remote_address: "10.0.0.7" } as ConnectorDTO;
    expect(describeTarget(connector, [{ id: 599001, name: "AHU sim" } as never])).toBe(
      "device 1234 at 10.0.0.7 (via AHU sim)"
    );
  });
});

describe("ConnectorsList", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(connectorsApi.list).mockResolvedValue([]);
    vi.mocked(connectorsApi.status).mockResolvedValue({});
    vi.mocked(devicesApi.list).mockResolvedValue([{ id: 599001, name: "AHU sim" } as never]);
    vi.mocked(connectorsApi.create).mockResolvedValue({} as never);
  });

  // The panel's own submit button, apart from the one that opens the panel.
  const addSubmit = () =>
    within(screen.getByRole("form", { name: "Add connector" })).getByRole("button", { name: "Add connector" });

  function renderList(entry = "/connectors") {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[entry]}>
          <ConnectorsList />
        </MemoryRouter>
      </QueryClientProvider>
    );
  }

  it("switches the add form to the bacnet fields", async () => {
    const user = userEvent.setup();
    renderList();
    await user.click(await screen.findByRole("button", { name: "Add connector" }));

    await user.selectOptions(screen.getByLabelText("Type"), "bacnet");

    expect(screen.queryByLabelText("URL")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Remote device instance")).toBeInTheDocument();
  });

  it("verifies TLS by default and warns once it's turned off", async () => {
    const user = userEvent.setup();
    renderList();
    await user.click(await screen.findByRole("button", { name: "Add connector" }));
    const verify = screen.getByLabelText("Verify TLS certificate");
    expect(verify).toBeChecked();
    expect(screen.queryByText(/self-signed endpoints/)).not.toBeInTheDocument();

    await user.click(verify);
    expect(screen.getByText(/self-signed endpoints/)).toBeInTheDocument();
    await user.type(screen.getByLabelText("Name"), "Lab");
    await user.type(screen.getByLabelText("URL"), "https://lab.local/api");
    await user.click(addSubmit());

    expect(connectorsApi.create).toHaveBeenCalledWith(
      expect.objectContaining({ url: "https://lab.local/api", verify_tls: false })
    );
  });

  it("clears the header rows after an add, so the next connector doesn't get them", async () => {
    const user = userEvent.setup();
    renderList();
    await user.click(await screen.findByRole("button", { name: "Add connector" }));
    await user.type(screen.getByLabelText("Name"), "Lab");
    await user.type(screen.getByLabelText("URL"), "https://lab.local/api");
    await user.click(screen.getByRole("button", { name: "Add header" }));
    await user.type(screen.getByLabelText("Header 1 name"), "X-Api-Key");
    await user.type(screen.getByLabelText("Header 1 value"), "secret");
    await user.click(addSubmit());
    expect(await screen.findByText("Connector added.")).toBeInTheDocument();
    expect(connectorsApi.create).toHaveBeenLastCalledWith(expect.objectContaining({ headers: { "X-Api-Key": "secret" } }));

    expect(screen.queryByLabelText("Header 1 value")).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("Name"), "Other");
    await user.type(screen.getByLabelText("URL"), "https://other.example/api");
    await user.click(addSubmit());
    await waitFor(() => expect(connectorsApi.create).toHaveBeenCalledTimes(2));
    expect(vi.mocked(connectorsApi.create).mock.lastCall![0]).not.toHaveProperty("headers");
  });

  it("drops the added line once the next connector is being typed", async () => {
    const user = userEvent.setup();
    renderList();
    await user.click(await screen.findByRole("button", { name: "Add connector" }));
    await user.type(screen.getByLabelText("Name"), "Lab");
    await user.type(screen.getByLabelText("URL"), "https://lab.local/api");
    await user.click(addSubmit());
    expect(await screen.findByText("Connector added.")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Name"), "Lab 2");
    expect(screen.queryByText("Connector added.")).not.toBeInTheDocument();
  });

  it("pre-fills a bacnet connector from the query string", async () => {
    const user = userEvent.setup();
    renderList("/connectors?type=bacnet&device=599001&instance=1234");

    expect(await screen.findByDisplayValue("1234")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Name"), "Remote");
    await user.click(addSubmit());

    expect(connectorsApi.create).toHaveBeenCalledWith(
      expect.objectContaining({ type: "bacnet", device_id: 599001, remote_device_instance: 1234 })
    );
  });

  const http = { id: 1, name: "Weather", type: "http-poll", enabled: true, url: "http://weather.local/api/v1/current?station=KXYZ&units=metric", poll_interval_seconds: 30, mapping_count: 4, write_mapping_count: 0 } as ConnectorDTO;
  const bacnet = { id: 2, name: "AHU remote", type: "bacnet", enabled: false, url: "", device_id: 599001, remote_device_instance: 1234, remote_address: null, poll_interval_seconds: 10, mapping_count: 2, write_mapping_count: 1 } as ConnectorDTO;

  it("lists connectors with a clipped source, a status pill and the last error", async () => {
    vi.mocked(connectorsApi.list).mockResolvedValue([http, bacnet]);
    vi.mocked(connectorsApi.status).mockResolvedValue({
      "1": { last_poll_at: "2026-10-27T12:00:00+00:00",last_success: false, last_error: "HTTP 503 from weather.local", consecutive_failures: 2 },
    });
    renderList();
    const source = await screen.findByText(http.url);
    expect(source).toHaveClass("bp-cell-clip");
    expect(source).toHaveAttribute("title", http.url);
    expect(screen.getByText("failing (2)")).toHaveClass("bp-pill--danger");
    expect(screen.getByText("HTTP 503 from weather.local")).toHaveClass("bp-cell-clip");
    expect(screen.getByText("disabled")).toHaveClass("bp-pill");
    expect(screen.getByText("30")).toHaveClass("bp-cell-num");
    expect(screen.getByText("2 read · 1 write")).toBeInTheDocument();
    const row = screen.getByText("Weather").closest("tr")!;
    expect(within(row).getByRole("link", { name: "Edit" })).toHaveAttribute("href", "/connectors/1");
  });

  it("searches and filters by type", async () => {
    vi.mocked(connectorsApi.list).mockResolvedValue([http, bacnet]);
    const user = userEvent.setup();
    renderList();
    await screen.findByText("Weather");
    expect(screen.getByText("2 connectors")).toBeInTheDocument();
    await user.selectOptions(screen.getByRole("combobox", { name: "Connector type" }), "bacnet");
    expect(screen.queryByText("Weather")).not.toBeInTheDocument();
    expect(screen.getByText("AHU remote")).toBeInTheDocument();
    await user.selectOptions(screen.getByRole("combobox", { name: "Connector type" }), "");
    await user.type(screen.getByRole("searchbox", { name: "Search connectors" }), "weather.local");
    expect(screen.getByText("Weather")).toBeInTheDocument();
    expect(screen.getByText("1 of 2 connectors")).toBeInTheDocument();
  });

  it("deletes a connector only after confirming", async () => {
    vi.mocked(connectorsApi.list).mockResolvedValue([http]);
    vi.mocked(connectorsApi.remove).mockResolvedValue(undefined as never);
    const user = userEvent.setup();
    renderList();
    const row = (await screen.findByText("Weather")).closest("tr")!;
    await user.click(within(row).getByRole("button", { name: "Delete" }));
    expect(screen.getByRole("alertdialog", { name: "Delete Weather?" })).toHaveAccessibleDescription(/its 4 mappings/);
    await user.click(screen.getByRole("button", { name: "Delete connector" }));
    expect(connectorsApi.remove).toHaveBeenCalledWith(1);
  });

  it("sends a username, password or token only for the auth type that uses it", () => {
    const base = { ...httpForm, authUsername: "lab", authPassword: "pw", authToken: "tok" };
    expect(connectorCreateBody({ ...base, authType: "none" })).not.toHaveProperty("auth_username");
    expect(connectorCreateBody({ ...base, authType: "none" })).not.toHaveProperty("auth_token");
    expect(connectorCreateBody({ ...base, authType: "basic" })).toMatchObject({ auth_username: "lab", auth_password: "pw" });
    expect(connectorCreateBody({ ...base, authType: "basic" })).not.toHaveProperty("auth_token");
    expect(connectorCreateBody({ ...base, authType: "bearer" })).toMatchObject({ auth_token: "tok" });
    expect(connectorCreateBody({ ...base, authType: "bearer" })).not.toHaveProperty("auth_username");
  });

  it("shows the auth fields only for the chosen auth type, keeping what was typed", async () => {
    const user = userEvent.setup();
    renderList();
    await user.click(await screen.findByRole("button", { name: "Add connector" }));
    expect(screen.queryByLabelText("Username")).not.toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Auth"), "basic");
    await user.type(screen.getByLabelText("Username"), "lab");
    expect(screen.queryByLabelText("Token")).not.toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Auth"), "bearer");
    expect(screen.queryByLabelText("Username")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Token")).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Auth"), "basic");
    expect(screen.getByLabelText("Username")).toHaveValue("lab");
  });

  it("sends the headers of an http-poll connector, a blank value as not set yet", () => {
    const headers = [
      { name: " X-Api-Key ", value: "k", savedName: null },
      { name: "Accept", value: "", savedName: null },
      { name: "", value: "", savedName: null },
    ];
    expect(connectorCreateBody({ ...httpForm, headers }).headers).toEqual({ "X-Api-Key": "k", Accept: null });
    expect(connectorCreateBody(httpForm)).not.toHaveProperty("headers");
    expect(connectorCreateBody({ ...form, headers })).not.toHaveProperty("headers");
  });

  it("adds a connector with a header", async () => {
    const user = userEvent.setup();
    renderList();
    await user.click(await screen.findByRole("button", { name: "Add connector" }));
    await user.type(screen.getByLabelText("Name"), "Lab");
    await user.type(screen.getByLabelText("URL"), "http://lab.local");
    await user.click(screen.getByRole("button", { name: "Add header" }));
    await user.type(screen.getByLabelText("Header 1 name"), "X-Api-Key");
    await user.type(screen.getByLabelText("Header 1 value"), "k");
    await user.click(addSubmit());
    expect(vi.mocked(connectorsApi.create).mock.calls[0][0]).toMatchObject({ headers: { "X-Api-Key": "k" } });
  });

  it("starts with the add panel closed and opens it from the button", async () => {
    const user = userEvent.setup();
    renderList();
    await user.click(await screen.findByRole("button", { name: "Add connector" }));
    expect(screen.getByRole("dialog", { name: "Add connector" })).toBeVisible();
  });

  it("opens the add panel at once when the URL names a type", async () => {
    renderList("/connectors?type=bacnet");
    expect(await screen.findByRole("dialog", { name: "Add connector" })).toBeVisible();
  });

  it("stays usable beside the panel and keeps a half-typed connector when closed", async () => {
    vi.mocked(connectorsApi.list).mockResolvedValue([
      { id: 1, name: "Weather", type: "http-poll", enabled: true, url: "http://w.local", poll_interval_seconds: 30, mapping_count: 0, write_mapping_count: 0 } as ConnectorDTO,
    ]);
    const user = userEvent.setup();
    renderList();
    await screen.findByRole("searchbox", { name: "Search connectors" });
    await user.click(screen.getByRole("button", { name: "Add connector" }));
    await user.type(screen.getByLabelText("Name"), "Half");
    await user.type(screen.getByRole("searchbox", { name: "Search connectors" }), "Wea");
    expect(screen.getByRole("searchbox", { name: "Search connectors" })).toHaveValue("Wea");
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog", { name: "Add connector" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Add connector" }));
    expect(screen.getByLabelText("Name")).toHaveValue("Half");
  });

  it("puts an add error under the add button", async () => {
    vi.mocked(connectorsApi.create).mockRejectedValue(new Error("name already used"));
    const user = userEvent.setup();
    renderList();
    await user.click(await screen.findByRole("button", { name: "Add connector" }));
    await user.type(screen.getByLabelText("Name"), "Lab");
    await user.type(screen.getByLabelText("URL"), "http://lab.local");
    await user.click(addSubmit());
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't add the connector: name already used");
  });

  it("names every control in the add form", async () => {
    const user = userEvent.setup();
    const { container } = renderList();
    await user.click(await screen.findByRole("button", { name: "Add connector" }));
    await user.selectOptions(screen.getByLabelText("Auth"), "basic");
    expectNamedControls(container);
  });

  it("shows no toolbar while the list is loading", async () => {
    vi.mocked(connectorsApi.list).mockReturnValue(new Promise(() => {}));
    renderList();
    expect(await screen.findByText("Loading connectors…")).toBeInTheDocument();
    expect(screen.queryByRole("searchbox")).not.toBeInTheDocument();
  });

  it("shows no toolbar when the list failed, and offers a retry", async () => {
    vi.mocked(connectorsApi.list).mockRejectedValue(new Error("HTTP 500"));
    renderList();
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load connectors: HTTP 500");
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.queryByRole("searchbox")).not.toBeInTheDocument();
  });
});
