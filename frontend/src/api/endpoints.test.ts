import { afterEach, describe, expect, it, vi } from "vitest";
import {
  PlanChangedError,
  apiKeysApi,
  authApi,
  configApi,
  diagnosticsApi,
  edeApi,
  loginSettingsApi,
  objectsApi,
  sampleApi,
  webServerApi,
  WEB_CERTIFICATE_URL,
} from "./endpoints";
import { ApiError } from "./http";

describe("diagnostics download", () => {
  it("names the mode in the query", () => {
    expect(diagnosticsApi.downloadUrl(false)).toBe("/api/diagnostics/download?real_values=false");
    expect(diagnosticsApi.downloadUrl(true)).toBe("/api/diagnostics/download?real_values=true");
  });
});

function respond(status: number, detail: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail }), { status, headers: { "content-type": "application/json" } })
    )
  );
}

const file = new File(["name\n"], "objects.csv");

describe("import errors", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("rejects a refused file with an ApiError carrying its status", async () => {
    respond(422, "row 2: unknown object type");
    const err = await objectsApi.previewCsv(1, file, "merge").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).not.toBeInstanceOf(PlanChangedError);
    expect((err as ApiError).status).toBe(422);
    expect((err as ApiError).message).toBe("row 2: unknown object type");
  });

  it("rejects a 409 without a digest as a plain ApiError", async () => {
    respond(409, "name clash");
    const err = await objectsApi.importCsv(1, file, "merge").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).not.toBeInstanceOf(PlanChangedError);
    expect((err as ApiError).status).toBe(409);
  });

  it("rejects another 409 after sending a digest as a plain ApiError", async () => {
    respond(409, "Demo: name 'AV-1' is used twice");
    const err = await objectsApi.importCsv(1, file, "sync", "abc").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).not.toBeInstanceOf(PlanChangedError);
    expect((err as ApiError).message).toBe("Demo: name 'AV-1' is used twice");
  });

  it("rejects a 409 after sending a digest as a PlanChangedError, which is an ApiError", async () => {
    respond(409, "the configuration changed since the preview; preview the file again before applying");
    const err = await configApi.import(file, false, "abc").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(PlanChangedError);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(409);
  });
});

describe("EDE uploads", () => {
  afterEach(() => vi.unstubAllGlobals());

  const ede = new File(["#keyname;x\n"], "plant.csv");
  const texts = new File(["1;Off;On\n"], "plant-states.csv");
  const devices = [{ instance: 1000, id: 1000, name: "Boiler" }];

  function capture(reply: unknown = {}) {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(reply), { status: 200, headers: { "content-type": "application/json" } })
    );
    vi.stubGlobal("fetch", fetchMock);
    return () => {
      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      return { url, form: init.body as FormData };
    };
  }

  it("plan sends the file, the state texts and the ticked devices as JSON", async () => {
    const sent = capture({ plans: [], vendor_name: null, plan_digest: "d" });
    await edeApi.plan(ede, texts, devices);
    const { url, form } = sent();
    expect(url).toBe("/api/ede/plan");
    expect((form.get("file") as File).name).toBe("plant.csv");
    expect((form.get("state_texts") as File).name).toBe("plant-states.csv");
    expect(JSON.parse(form.get("devices") as string)).toEqual(devices);
  });

  it("read leaves out a state-texts file that wasn't chosen", async () => {
    const sent = capture({ devices: [] });
    await edeApi.read(ede, null);
    const { url, form } = sent();
    expect(url).toBe("/api/ede/read");
    expect(form.has("state_texts")).toBe(false);
  });

  it("create sends the digest, and a digest 409 is a PlanChangedError", async () => {
    const sent = capture({ device_ids: [1000] });
    await edeApi.create(ede, null, devices, "abc");
    expect(sent().form.get("plan_digest")).toBe("abc");
    respond(409, "the file or its plan changed since the preview; preview it again");
    const err = await edeApi.create(ede, null, devices, "abc").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(PlanChangedError);
  });

  it("a create 409 naming a device is a plain ApiError", async () => {
    respond(409, "device 1000 (Boiler): device 1000 already exists");
    const err = await edeApi.create(ede, null, devices, "abc").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).not.toBeInstanceOf(PlanChangedError);
  });

  it("the object import sends ede_device in the query and the state texts in the form", async () => {
    const sent = capture({ dry_run: true });
    await objectsApi.previewCsv(5, ede, "sync", { stateTexts: texts, edeDevice: 2000 });
    const { url, form } = sent();
    expect(new URL(url, "http://x").searchParams.get("ede_device")).toBe("2000");
    expect((form.get("state_texts") as File).name).toBe("plant-states.csv");
  });

  it("a plain CSV preview sends neither", async () => {
    const sent = capture({ dry_run: true });
    await objectsApi.previewCsv(5, ede, "merge");
    const { url, form } = sent();
    expect(url).not.toContain("ede_device");
    expect(form.has("state_texts")).toBe(false);
  });

  it("the apply sends ede_device too", async () => {
    const sent = capture({ imported: 1 });
    await objectsApi.importCsv(5, ede, "merge", "abc", { edeDevice: 1000 });
    expect(new URL(sent().url, "http://x").searchParams.get("ede_device")).toBe("1000");
  });
});

describe("sample building", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("posts whether to start the devices", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ devices: [] }), { status: 201, headers: { "content-type": "application/json" } })
    );
    vi.stubGlobal("fetch", fetchMock);
    await sampleApi.load(false);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/sample/load");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ start: false });
  });
});

describe("login and API key endpoints", () => {
  afterEach(() => vi.unstubAllGlobals());

  function mock(status = 200, body: unknown = {}) {
    const fetchMock = vi.fn().mockResolvedValue(
      status === 204
        ? new Response(null, { status })
        : new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } })
    );
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  function lastCall(fetchMock: ReturnType<typeof vi.fn>) {
    const [url, init] = fetchMock.mock.calls[0];
    return { url, method: init?.method ?? "GET", body: init?.body ? JSON.parse(init.body) : undefined };
  }

  it.each([
    ["state", () => authApi.state(), "GET", "/api/auth/state", undefined],
    ["login", () => authApi.login({ username: "a", password: "b" }), "POST", "/api/auth/login", { username: "a", password: "b" }],
    ["logout", () => authApi.logout(), "POST", "/api/auth/logout", undefined],
    [
      "setup login",
      () => authApi.setup({ mode: "login", username: "a", password: "b" }),
      "POST",
      "/api/auth/setup",
      { mode: "login", username: "a", password: "b" },
    ],
    ["setup none", () => authApi.setup({ mode: "none", confirm: true }), "POST", "/api/auth/setup", { mode: "none", confirm: true }],
    ["get login", () => loginSettingsApi.get(), "GET", "/api/settings/login", undefined],
    [
      "update login",
      () => loginSettingsApi.update({ new_password: "n", current_password: "c", revoke_api_keys: true }),
      "PUT",
      "/api/settings/login",
      { new_password: "n", current_password: "c", revoke_api_keys: true },
    ],
    [
      "enable",
      () => loginSettingsApi.enable({ username: "a", password: "b", revoke_api_keys: true }),
      "POST",
      "/api/settings/login/enable",
      { username: "a", password: "b", revoke_api_keys: true },
    ],
    ["disable", () => loginSettingsApi.disable({ current_password: "c" }), "POST", "/api/settings/login/disable", { current_password: "c" }],
    ["list keys", () => apiKeysApi.list(), "GET", "/api/settings/api-keys", undefined],
    [
      "create key",
      () => apiKeysApi.create("grafana", "pw"),
      "POST",
      "/api/settings/api-keys",
      { name: "grafana", current_password: "pw" },
    ],
    ["revoke key", () => apiKeysApi.revoke(7), "DELETE", "/api/settings/api-keys/7", undefined],
    ["get web server", () => webServerApi.get(), "GET", "/api/settings/web", undefined],
    [
      "set listen address",
      () => webServerApi.setListenAddress("10.0.0.5"),
      "PUT",
      "/api/settings/web",
      { listen_address: "10.0.0.5" },
    ],
    ["self-signed", () => webServerApi.useSelfSigned(), "POST", "/api/settings/web/certificate/self-signed", undefined],
    [
      "upload certificate",
      () => webServerApi.uploadCertificate({ certificate_pem: "c", key_pem: "k" }),
      "POST",
      "/api/settings/web/certificate",
      { certificate_pem: "c", key_pem: "k" },
    ],
  ])("%s", async (_name, call, method, url, body) => {
    const fetchMock = mock(method === "DELETE" || url.endsWith("logout") ? 204 : 200);
    await call();
    expect(lastCall(fetchMock)).toEqual({ url, method, body });
  });
});

describe("web certificate download", () => {
  it("is a plain link to the public certificate", () => {
    expect(WEB_CERTIFICATE_URL).toBe("/api/settings/web/certificate.pem");
  });
});
