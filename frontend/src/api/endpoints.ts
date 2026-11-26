import { ApiError, api, formatErrorDetail } from "./http";
import type {
  ApiKeyInfo,
  AuthState,
  BacnetObjectDTO,
  Capabilities,
  CloneJob,
  ClonePlanResult,
  DeviceLiveDTO,
  ObjectLiveDTO,
  TrendRangeDTO,
  ConfigSyncPlan,
  CsvImportPlan,
  ConnectorDTO,
  ConnectorMappingDTO,
  ConnectorPreviewDTO,
  ConnectorStatusDTO,
  ConnectorWriteMappingDTO,
  DeviceClockState,
  DeviceProcessStatus,
  RestartAllResult,
  DeviceSummary,
  DiscoveredDevice,
  EdeDeviceChoice,
  EdePlanResult,
  EdeReadResult,
  LogEntry,
  LoginSettings,
  NetworkSettingsDTO,
  NewApiKey,
  VersionInfo,
  WebServerSettings,
  ObjectWithDevice,
  PortSuggestion,
  RemoteIdentity,
  RemoteObject,
  RemotePointRequest,
  RemoteTarget,
  SampleLoadResult,
  SamplePlan,
  TrafficEntry,
  TrendStorageDTO,
} from "./types";

export const devicesApi = {
  list: () => api.get<DeviceSummary[]>("/api/devices"),
  create: (body: {
    id: number;
    name: string;
    description?: string;
    location?: string;
    vendor_identifier?: number;
    vendor_name?: string;
  }) =>
    api.post<DeviceSummary>("/api/devices", body),
  get: (id: number) => api.get<DeviceSummary>(`/api/devices/${id}`),
  update: (id: number, body: Record<string, unknown>) => api.put<DeviceSummary>(`/api/devices/${id}`, body),
  remove: (id: number) => api.del<void>(`/api/devices/${id}`),
  portSuggestion: (id: number) => api.get<PortSuggestion>(`/api/devices/${id}/port-suggestion`),
  setClock: (id: number, body: { device_time: string } | { offset_seconds: number }) =>
    api.put<DeviceClockState>(`/api/devices/${id}/clock`, body),
};

// An apply that sent a preview's plan_digest got a 409: the plan changed
// since the preview. A 409 without a digest is a refused file, a plain ApiError.
export class PlanChangedError extends ApiError {
  constructor(message: string) {
    super(message, 409);
    this.name = "PlanChangedError";
  }
}

// Form fields sent beside the file; null and undefined ones are left out.
type ImportFields = Record<string, File | string | null | undefined>;

async function postImport<T>(url: string, file: File, digestSent: boolean, fields: ImportFields = {}): Promise<T> {
  const form = new FormData();
  form.append("file", file);
  for (const [name, value] of Object.entries(fields)) {
    if (value !== null && value !== undefined) form.append(name, value);
  }
  const res = await fetch(url, { method: "POST", body: form });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}) as { detail?: unknown });
    const message = body.detail === undefined ? res.statusText : formatErrorDetail(body.detail);
    // An apply can be refused with 409 for other reasons too (a name clash
    // found after the flush); only the digest check says this.
    const planChanged = res.status === 409 && digestSent && message.includes("changed since the preview");
    throw planChanged ? new PlanChangedError(message) : new ApiError(message, res.status);
  }
  return res.json();
}

export type EdeImportOptions = { stateTexts?: File | null; edeDevice?: number };

export const objectsApi = {
  list: (deviceId: number) => api.get<BacnetObjectDTO[]>(`/api/devices/${deviceId}/objects`),
  get: (deviceId: number, objectId: number) =>
    api.get<BacnetObjectDTO>(`/api/devices/${deviceId}/objects/${objectId}`),
  create: (deviceId: number, body: Record<string, unknown>) =>
    api.post<BacnetObjectDTO>(`/api/devices/${deviceId}/objects`, body),
  update: (deviceId: number, objectId: number, body: Record<string, unknown>) =>
    api.put<BacnetObjectDTO>(`/api/devices/${deviceId}/objects/${objectId}`, body),
  remove: (deviceId: number, objectId: number) =>
    api.del<void>(`/api/devices/${deviceId}/objects/${objectId}`),
  // randomPhase: each object gets its own random phase for a periodic
  // simulator in `patch`.
  batchUpdate: (
    deviceId: number,
    objectIds: number[],
    patch: Record<string, unknown>,
    options: { randomPhase?: boolean } = {}
  ) =>
    api.patch<BacnetObjectDTO[]>(`/api/devices/${deviceId}/objects/batch`, {
      object_ids: objectIds,
      patch,
      ...(options.randomPhase ? { random_phase: true } : {}),
    }),
  batchDelete: (deviceId: number, objectIds: number[]) =>
    api.del<{ deleted: number }>(`/api/devices/${deviceId}/objects/batch`, {
      object_ids: objectIds,
    }),
  // mode "merge" (default): additive only, rejects on conflict. mode "sync"
  // (file-based config): the file becomes this device's complete object
  // list, and objects it doesn't mention are deleted. The tab previews first
  // and sends the preview's digest back, as the JSON config import does.
  // An EDE file (told by its content) may come with a state-texts file and,
  // when it has several devices, the instance of the one to import.
  previewCsv: (deviceId: number, file: File, mode: "merge" | "sync", ede: EdeImportOptions = {}) => {
    const params = new URLSearchParams({ mode, dry_run: "true" });
    if (ede.edeDevice !== undefined) params.set("ede_device", String(ede.edeDevice));
    return postImport<CsvImportPlan>(`/api/devices/${deviceId}/objects/import?${params}`, file, false, {
      state_texts: ede.stateTexts,
    });
  },
  importCsv: (
    deviceId: number,
    file: File,
    mode: "merge" | "sync" = "merge",
    planDigest?: string,
    ede: EdeImportOptions = {}
  ) => {
    const params = new URLSearchParams({ mode });
    if (planDigest !== undefined) params.set("plan_digest", planDigest);
    if (ede.edeDevice !== undefined) params.set("ede_device", String(ede.edeDevice));
    return postImport<{ imported: number } | { created: string[]; updated: string[]; deleted: string[] }>(
      `/api/devices/${deviceId}/objects/import?${params}`,
      file,
      planDigest !== undefined,
      { state_texts: ede.stateTexts }
    );
  },
  exportCsvUrl: (deviceId: number) => `/api/devices/${deviceId}/objects/export.csv`,
};

// Whole-server JSON config. Unlike the per-device CSV path above, import
// defaults to a dry run: the caller sees the create/update/delete plan
// before committing, since a whole-server sync's blast radius is much
// bigger than one device's object list.
// Apply sends back the preview's plan_digest; a 409 means the server's plan
// changed since the preview, so the page previews again.
export const configApi = {
  exportUrl: () => "/api/config/export",
  import: (file: File, dryRun: boolean, planDigest?: string): Promise<ConfigSyncPlan> => {
    const params = new URLSearchParams({ dry_run: String(dryRun) });
    if (planDigest !== undefined) params.set("plan_digest", planDigest);
    return postImport<ConfigSyncPlan>(`/api/config/import?${params}`, file, planDigest !== undefined);
  },
};

// Objects across every device, for pickers that aren't scoped to one device
// (the connector mapping picker) -- see ObjectWithDevice's doc comment.
export async function fetchAllObjectsWithDevice(): Promise<ObjectWithDevice[]> {
  const devices = await devicesApi.list();
  const perDevice = await Promise.all(devices.map((d) => objectsApi.list(d.id)));
  return perDevice.flatMap((objs, i) =>
    objs.map((o) => ({ ...o, deviceName: devices[i].name }))
  );
}

export const trendApi = {
  exportCsvUrl: (deviceId: number, objectId: number) =>
    `/api/devices/${deviceId}/objects/${objectId}/trend/export.csv`,
  range: (
    deviceId: number,
    objectId: number,
    { last, start, end, maxPoints }: { last?: number; start?: string; end?: string; maxPoints: number }
  ) => {
    const params = new URLSearchParams({ max_points: String(maxPoints) });
    if (last !== undefined) params.set("last", String(last));
    if (start) params.set("start", start);
    if (end) params.set("end", end);
    return api.get<TrendRangeDTO>(`/api/devices/${deviceId}/objects/${objectId}/trend/range?${params}`);
  },
};

export const liveApi = {
  device: (deviceId: number) => api.get<DeviceLiveDTO>(`/api/devices/${deviceId}/live`),
  object: (deviceId: number, objectId: number) =>
    api.get<ObjectLiveDTO>(`/api/devices/${deviceId}/objects/${objectId}/live`),
};

export const trendStorageApi = {
  get: () => api.get<TrendStorageDTO>("/api/trends/storage"),
};

export const connectorsApi = {
  list: () => api.get<ConnectorDTO[]>("/api/connectors"),
  create: (body: Record<string, unknown>) => api.post<ConnectorDTO>("/api/connectors", body),
  get: (id: number) => api.get<ConnectorDTO>(`/api/connectors/${id}`),
  update: (id: number, body: Record<string, unknown>) =>
    api.put<ConnectorDTO>(`/api/connectors/${id}`, body),
  remove: (id: number) => api.del<void>(`/api/connectors/${id}`),
  status: () => api.get<Record<string, ConnectorStatusDTO>>("/api/connectors/status"),
  preview: (id: number) => api.post<ConnectorPreviewDTO>(`/api/connectors/${id}/preview`),
  mappings: {
    list: (connectorId: number) =>
      api.get<ConnectorMappingDTO[]>(`/api/connectors/${connectorId}/mappings`),
    create: (connectorId: number, body: Record<string, unknown>) =>
      api.post<ConnectorMappingDTO>(`/api/connectors/${connectorId}/mappings`, body),
    update: (connectorId: number, mappingId: number, body: Record<string, unknown>) =>
      api.put<ConnectorMappingDTO>(`/api/connectors/${connectorId}/mappings/${mappingId}`, body),
    remove: (connectorId: number, mappingId: number) =>
      api.del<void>(`/api/connectors/${connectorId}/mappings/${mappingId}`),
  },
  writeMappings: {
    list: (connectorId: number) =>
      api.get<ConnectorWriteMappingDTO[]>(`/api/connectors/${connectorId}/write-mappings`),
    create: (connectorId: number, body: Record<string, unknown>) =>
      api.post<ConnectorWriteMappingDTO>(`/api/connectors/${connectorId}/write-mappings`, body),
    update: (connectorId: number, mappingId: number, body: Record<string, unknown>) =>
      api.put<ConnectorWriteMappingDTO>(`/api/connectors/${connectorId}/write-mappings/${mappingId}`, body),
    remove: (connectorId: number, mappingId: number) =>
      api.del<void>(`/api/connectors/${connectorId}/write-mappings/${mappingId}`),
  },
};

// The device's BACnet client tool:
// each call is answered by that device's own process, so it needs the
// device running (409 otherwise).
export const clientToolApi = {
  whoIs: (deviceId: number, body: { low?: number; high?: number; window_seconds?: number }) =>
    api.post<DiscoveredDevice[]>(`/api/devices/${deviceId}/client/who-is`, body),
  objectList: (deviceId: number, body: RemoteTarget) =>
    api.post<RemoteObject[]>(`/api/devices/${deviceId}/client/object-list`, body),
  identify: (deviceId: number, body: RemoteTarget) =>
    api.post<RemoteIdentity>(`/api/devices/${deviceId}/client/identify`, body),
  read: (deviceId: number, body: RemotePointRequest) =>
    api.post<{ value: unknown }>(`/api/devices/${deviceId}/client/read`, body),
  write: (deviceId: number, body: RemotePointRequest & { value: unknown; priority?: number | null }) =>
    api.post<{ ok: true }>(`/api/devices/${deviceId}/client/write`, body),
};

export const cloneApi = {
  start: (deviceId: number, body: RemoteTarget) =>
    api.post<{ job_id: number; status: string }>(`/api/devices/${deviceId}/client/clone`, body),
  // 404 when the device has no clone job.
  get: (deviceId: number) => api.get<CloneJob>(`/api/devices/${deviceId}/client/clone`),
  cancel: (deviceId: number, jobId: number) =>
    api.post<{ job_id: number; status: string; cancel_requested: boolean }>(
      `/api/devices/${deviceId}/client/clone/${jobId}/cancel`
    ),
  discard: (deviceId: number, jobId: number) => api.del<void>(`/api/devices/${deviceId}/client/clone/${jobId}`),
  // id and name left out = the controller's own.
  plan: (deviceId: number, jobId: number, body: { id?: number; name?: string }) =>
    api.post<ClonePlanResult>(`/api/devices/${deviceId}/client/clone/${jobId}/plan`, body),
  create: (deviceId: number, jobId: number, body: { id: number; name: string; bind_address?: string | null }) =>
    api.post<{ device_id: number }>(`/api/devices/${deviceId}/client/clone/${jobId}/create`, body),
};

// An EDE file as new, stopped devices. The files go with every call: the
// server never stores them. Create sends the preview's digest back; a 409 then
// means the plan changed.
export const edeApi = {
  read: (file: File, stateTexts?: File | null) =>
    postImport<EdeReadResult>("/api/ede/read", file, false, { state_texts: stateTexts }),
  plan: (file: File, stateTexts: File | null, devices: EdeDeviceChoice[]) =>
    postImport<EdePlanResult>("/api/ede/plan", file, false, {
      state_texts: stateTexts,
      devices: JSON.stringify(devices),
    }),
  create: (file: File, stateTexts: File | null, devices: EdeDeviceChoice[], planDigest?: string) =>
    postImport<{ device_ids: number[] }>("/api/ede/create", file, planDigest !== undefined, {
      state_texts: stateTexts,
      devices: JSON.stringify(devices),
      plan_digest: planDigest,
    }),
};

// The packaged sample building: the plan for this server, then an add-only load.
export const sampleApi = {
  plan: () => api.get<SamplePlan>("/api/sample"),
  load: (start: boolean) => api.post<SampleLoadResult>("/api/sample/load", { start }),
};

export const engineApi = {
  status: () => api.get<DeviceProcessStatus[]>("/api/engine/status"),
  start: (deviceId: number) => api.post<DeviceProcessStatus[]>("/api/engine/start", { device_id: deviceId }),
  stop: (deviceId: number) => api.post<DeviceProcessStatus[]>("/api/engine/stop", { device_id: deviceId }),
  // Reloads the live device's objects from the DB without changing which
  // device is live -- the fix for "I added/edited an object but it's not
  // live on the wire", since new/changed objects only take effect for
  // objects that existed at the last engine.start()/restart() call.
  restart: (deviceId: number) => api.post<DeviceProcessStatus[]>("/api/engine/restart", { device_id: deviceId }),
  // Restarts every running device (stopped/crashed ones are left alone), so
  // settings only read at start -- network role, BBMD peers -- take effect.
  restartAll: () => api.post<RestartAllResult>("/api/engine/restart-all"),
  // Exits the web process for the service manager to restart; devices keep running.
  restartUi: () => api.post<{ restarting: boolean }>("/api/engine/restart-ui"),
};

export const settingsApi = {
  get: () => api.get<NetworkSettingsDTO>("/api/settings/network"),
  update: (body: Record<string, unknown>) => api.put<NetworkSettingsDTO>("/api/settings/network", body),
};

export const authApi = {
  state: () => api.get<AuthState>("/api/auth/state"),
  setup: (body: { mode: "login"; username: string; password: string } | { mode: "none"; confirm: true }) =>
    api.post<{ mode: string }>("/api/auth/setup", body),
  login: (body: { username: string; password: string }) => api.post<AuthState>("/api/auth/login", body),
  logout: () => api.post<void>("/api/auth/logout"),
};

export const loginSettingsApi = {
  get: () => api.get<LoginSettings>("/api/settings/login"),
  // A password change needs current_password; a rename alone does not.
  // revoke_api_keys goes with a new password.
  update: (body: { username?: string; new_password?: string; current_password?: string; revoke_api_keys?: boolean }) =>
    api.put<LoginSettings>("/api/settings/login", body),
  // revoke_api_keys revokes every existing key in the same save.
  enable: (body: { username: string; password: string; revoke_api_keys?: boolean }) =>
    api.post<LoginSettings>("/api/settings/login/enable", body),
  disable: (body: { current_password: string }) => api.post<LoginSettings>("/api/settings/login/disable", body),
};

export const apiKeysApi = {
  list: () => api.get<ApiKeyInfo[]>("/api/settings/api-keys"),
  // current_password is needed while the login is on.
  create: (name: string, currentPassword?: string) =>
    api.post<NewApiKey>("/api/settings/api-keys", { name, current_password: currentPassword }),
  revoke: (id: number) => api.del<void>(`/api/settings/api-keys/${id}`),
};

export const WEB_CERTIFICATE_URL = "/api/settings/web/certificate.pem";

export const webServerApi = {
  get: () => api.get<WebServerSettings>("/api/settings/web"),
  // Takes effect when the web process restarts.
  setListenAddress: (address: string) => api.put<WebServerSettings>("/api/settings/web", { listen_address: address }),
  // Regenerates BACpack's own certificate, or goes back to it from an uploaded one.
  useSelfSigned: () => api.post<WebServerSettings>("/api/settings/web/certificate/self-signed"),
  uploadCertificate: (body: { certificate_pem: string; key_pem: string }) =>
    api.post<WebServerSettings>("/api/settings/web/certificate", body),
};

export const trafficApi = {
  recent: (deviceId: number, limit = 200) =>
    api.get<TrafficEntry[]>(`/api/traffic?device_id=${deviceId}&limit=${limit}`),
};

export const logsApi = {
  list: (params: { device_id?: number; level?: string; limit?: number } = {}) => {
    const search = new URLSearchParams();
    if (params.device_id !== undefined) search.set("device_id", String(params.device_id));
    if (params.level !== undefined) search.set("level", params.level);
    if (params.limit !== undefined) search.set("limit", String(params.limit));
    const qs = search.toString();
    return api.get<LogEntry[]>(`/api/logs${qs ? `?${qs}` : ""}`);
  },
};

export const unitsApi = {
  // Real bacpypes3 EngineeringUnits names, for the units dropdown -- see
  // bacnet.list_engineering_units. Fetched rather than hardcoded so the
  // frontend can't drift from whatever's actually accepted.
  list: () => api.get<{ units: string[] }>("/api/units"),
};

export const versionApi = {
  // The installed bacpack version, for the header -- fetched rather than
  // baked into the bundle at build time so it can't drift from what's
  // actually running server-side.
  get: () => api.get<VersionInfo>("/api/version"),
};

export const capabilitiesApi = {
  // What every BACpack device advertises over BACnet -- derived server-side
  // from the same code the wire uses, so it can't drift. Static while the
  // server runs, so callers fetch it once (no polling).
  get: () => api.get<Capabilities>("/api/capabilities"),
};

export const diagnosticsApi = {
  // A plain link target, not a fetch: the browser saves the zip itself and
  // sends the session cookie as for any page.
  downloadUrl: (realValues: boolean) => `/api/diagnostics/download?real_values=${realValues}`,
};
