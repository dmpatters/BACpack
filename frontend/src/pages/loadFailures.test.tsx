// A list or settings page whose load fails says so, rather than rendering as
// empty data ("No devices yet.") or a blank page.
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import Dashboard from "./Dashboard";
import ConnectorsList from "./ConnectorsList";
import ConnectorDetail from "./ConnectorDetail";
import NetworkSettings from "./NetworkSettings";
import TrendStorage from "./TrendStorage";
import Logs from "./Logs";
import BacnetConnectorDetail from "./BacnetConnectorDetail";
import DeviceObjectsTab from "../components/DeviceObjectsTab";
import DeviceTrafficTab from "../components/DeviceTrafficTab";
import DeviceLogsTab from "../components/DeviceLogsTab";
import type { ConnectorDTO } from "../api/types";
import {
  connectorsApi,
  devicesApi,
  engineApi,
  fetchAllObjectsWithDevice,
  logsApi,
  objectsApi,
  settingsApi,
  trafficApi,
  trendStorageApi,
} from "../api/endpoints";

vi.mock("../api/endpoints", () => ({
  devicesApi: { list: vi.fn() },
  engineApi: { status: vi.fn(), start: vi.fn(), stop: vi.fn(), restartAll: vi.fn() },
  versionApi: { get: vi.fn().mockResolvedValue({ version: "0.32.1", service: false }) },
  connectorsApi: {
    list: vi.fn(),
    get: vi.fn(),
    status: vi.fn(),
    preview: vi.fn(),
    create: vi.fn(),
    mappings: { list: vi.fn() },
    writeMappings: { list: vi.fn() },
  },
  fetchAllObjectsWithDevice: vi.fn(),
  settingsApi: { get: vi.fn(), update: vi.fn() },
  trendStorageApi: { get: vi.fn() },
  logsApi: { list: vi.fn() },
  objectsApi: { list: vi.fn() },
  unitsApi: { list: vi.fn() },
  trafficApi: { recent: vi.fn() },
  clientToolApi: { objectList: vi.fn() },
  authApi: { state: vi.fn().mockResolvedValue({ mode: "login", signed_in: true, username: "admin" }) },
}));

const boom = () => Promise.reject(new Error("HTTP 500: database is locked"));

function renderAt(element: ReactNode, path = "/", route = "/") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path={route} element={element} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe("a failed load", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(engineApi.status).mockResolvedValue([] as never);
    vi.mocked(connectorsApi.status).mockResolvedValue({} as never);
  });

  it("on the Dashboard isn't 'No devices yet.'", async () => {
    vi.mocked(devicesApi.list).mockImplementation(boom);
    renderAt(<Dashboard />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load devices: HTTP 500: database is locked");
    expect(screen.queryByText("No devices yet.")).not.toBeInTheDocument();
  });

  it("on the Connectors list isn't 'No connectors yet.'", async () => {
    vi.mocked(connectorsApi.list).mockImplementation(boom);
    vi.mocked(devicesApi.list).mockResolvedValue([] as never);
    renderAt(<ConnectorsList />);
    expect(await screen.findByText(/Couldn't load connectors/)).toBeInTheDocument();
    expect(screen.queryByText("No connectors yet.")).not.toBeInTheDocument();
  });

  it("on Network settings isn't a blank page", async () => {
    vi.mocked(settingsApi.get).mockImplementation(boom);
    vi.mocked(devicesApi.list).mockResolvedValue([] as never);
    renderAt(<NetworkSettings />);
    expect(await screen.findByText(/Couldn't load network settings/)).toBeInTheDocument();
  });

  it("on Trend storage isn't a blank page", async () => {
    vi.mocked(settingsApi.get).mockResolvedValue({} as never);
    vi.mocked(trendStorageApi.get).mockImplementation(boom);
    renderAt(<TrendStorage />);
    expect(await screen.findByText(/Couldn't load trend storage/)).toBeInTheDocument();
  });

  it("on Logs isn't 'No log entries recorded yet.'", async () => {
    vi.mocked(devicesApi.list).mockResolvedValue([] as never);
    vi.mocked(logsApi.list).mockImplementation(boom);
    renderAt(<Logs />);
    expect(await screen.findByText(/Couldn't load log entries/)).toBeInTheDocument();
    expect(screen.queryByText(/No log entries recorded yet/)).not.toBeInTheDocument();
  });

  it("of the objects on a connector page doesn't claim every object is fed", async () => {
    vi.mocked(connectorsApi.get).mockResolvedValue({
      id: 42, name: "C", url: "http://example.com", enabled: true, poll_interval_seconds: 60,
      timeout_seconds: 10, response_format: "json", auth_type: "none", auth_username: null, header_names: [], missing_credentials: [], verify_tls: true,
    } as never);
    vi.mocked(connectorsApi.mappings.list).mockResolvedValue([]);
    vi.mocked(connectorsApi.preview).mockResolvedValue({ ok: true, response_format: "json", fields: [] });
    vi.mocked(fetchAllObjectsWithDevice).mockImplementation(boom);
    renderAt(<ConnectorDetail />, "/connectors/42", "/connectors/:connectorId");
    expect(await screen.findByText(/Couldn't load objects/)).toBeInTheDocument();
    expect(screen.queryByText(/already fed/)).not.toBeInTheDocument();
  });

  it("while still loading the objects on a connector page doesn't claim every object is fed", async () => {
    vi.mocked(connectorsApi.get).mockResolvedValue({
      id: 42, name: "C", url: "http://example.com", enabled: true, poll_interval_seconds: 60,
      timeout_seconds: 10, response_format: "json", auth_type: "none", auth_username: null, header_names: [], missing_credentials: [], verify_tls: true,
    } as never);
    vi.mocked(connectorsApi.mappings.list).mockResolvedValue([]);
    vi.mocked(connectorsApi.preview).mockResolvedValue({ ok: true, response_format: "json", fields: [] });
    vi.mocked(fetchAllObjectsWithDevice).mockReturnValue(new Promise(() => {}) as never);
    renderAt(<ConnectorDetail />, "/connectors/42", "/connectors/:connectorId");
    await screen.findByText("C");
    expect(screen.queryByText(/already fed/)).not.toBeInTheDocument();
  });
});

describe("a failed load on a device tab or connector list", () => {
  const bacnetConnector = {
    id: 7, name: "To AHU", type: "bacnet", enabled: true, url: "", poll_interval_seconds: 30,
    timeout_seconds: 10, device_id: 599001, remote_device_instance: 1234, remote_address: null,
    rewrite_interval_seconds: 60, mapping_count: 0, write_mapping_count: 0,
  } as ConnectorDTO;

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(engineApi.status).mockResolvedValue([] as never);
    vi.mocked(connectorsApi.status).mockResolvedValue({} as never);
    vi.mocked(connectorsApi.mappings.list).mockResolvedValue([]);
    vi.mocked(connectorsApi.writeMappings.list).mockResolvedValue([] as never);
    vi.mocked(objectsApi.list).mockResolvedValue([] as never);
  });

  it("on the Objects tab isn't 'No objects yet.'", async () => {
    vi.mocked(objectsApi.list).mockImplementation(boom);
    renderAt(<DeviceObjectsTab deviceId={599001} />);
    expect(await screen.findByText(/Couldn't load objects/)).toBeInTheDocument();
    expect(screen.queryByText("No objects yet.")).not.toBeInTheDocument();
  });

  it("on the Traffic tab isn't 'No traffic recorded yet.'", async () => {
    vi.mocked(trafficApi.recent).mockImplementation(boom);
    renderAt(<DeviceTrafficTab deviceId={599001} />);
    expect(await screen.findByText(/Couldn't load traffic/)).toBeInTheDocument();
    expect(screen.queryByText("No traffic recorded yet.")).not.toBeInTheDocument();
  });

  it("on a device's Logs tab isn't 'No log entries recorded yet.'", async () => {
    vi.mocked(logsApi.list).mockImplementation(boom);
    renderAt(<DeviceLogsTab deviceId={599001} />);
    expect(await screen.findByText(/Couldn't load log entries/)).toBeInTheDocument();
    expect(screen.queryByText(/No log entries recorded yet/)).not.toBeInTheDocument();
  });

  it("of a BACnet connector's mappings isn't 'No ... mappings yet.'", async () => {
    vi.mocked(connectorsApi.mappings.list).mockImplementation(boom);
    vi.mocked(connectorsApi.writeMappings.list).mockImplementation(boom);
    renderAt(<BacnetConnectorDetail connector={bacnetConnector} />);
    expect(await screen.findByText(/Couldn't load read mappings/)).toBeInTheDocument();
    expect(await screen.findByText(/Couldn't load write mappings/)).toBeInTheDocument();
    expect(screen.queryByText(/No read mappings yet/)).not.toBeInTheDocument();
    expect(screen.queryByText(/No write mappings yet/)).not.toBeInTheDocument();
  });

  it("of a BACnet connector's local objects doesn't say the device has none", async () => {
    vi.mocked(objectsApi.list).mockImplementation(boom);
    renderAt(<BacnetConnectorDetail connector={bacnetConnector} />);
    expect((await screen.findAllByText(/Couldn't load objects/)).length).toBeGreaterThan(0);
    expect(screen.queryByText(/no objects to map/)).not.toBeInTheDocument();
  });

  it("of an HTTP connector's mappings isn't 'No mappings yet.'", async () => {
    vi.mocked(connectorsApi.get).mockResolvedValue({
      id: 42, name: "C", url: "http://example.com", enabled: true, poll_interval_seconds: 60,
      timeout_seconds: 10, response_format: "json", auth_type: "none", auth_username: null, header_names: [], missing_credentials: [], verify_tls: true,
    } as never);
    vi.mocked(connectorsApi.preview).mockResolvedValue({ ok: true, response_format: "json", fields: [] });
    vi.mocked(fetchAllObjectsWithDevice).mockResolvedValue([] as never);
    vi.mocked(connectorsApi.mappings.list).mockImplementation(boom);
    renderAt(<ConnectorDetail />, "/connectors/42", "/connectors/:connectorId");
    expect(await screen.findByText(/Couldn't load mappings/)).toBeInTheDocument();
    expect(screen.queryByText("No mappings yet.")).not.toBeInTheDocument();
  });
});
