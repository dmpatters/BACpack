import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, formatErrorDetail, isNotFound, setAuthHandlers } from "./http";

describe("formatErrorDetail", () => {
  it("passes a string through", () => {
    expect(formatErrorDetail("object 76 is already fed")).toBe("object 76 is already fed");
  });

  it("formats FastAPI's validation list", () => {
    expect(
      formatErrorDetail([
        { type: "value_error", loc: ["body"], msg: "Value error, low must not be above high", input: {} },
        { type: "less_than_equal", loc: ["body", "high"], msg: "Input should be less than or equal to 4194303" },
      ])
    ).toBe("low must not be above high; high: Input should be less than or equal to 4194303");
  });

  it("names the innermost field, skipping list indexes", () => {
    expect(formatErrorDetail([{ loc: ["body", "weekly_schedule", 0, 1, "value"], msg: "bad" }])).toBe("value: bad");
  });

  it("falls back to JSON for anything else", () => {
    expect(formatErrorDetail({ reason: "x" })).toBe('{"reason":"x"}');
    expect(formatErrorDetail([])).toBe("[]");
  });
});

describe("api errors", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("throws the formatted 422 detail", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: [{ loc: ["body", "low"], msg: "Value error, too big" }] }), {
          status: 422,
          headers: { "content-type": "application/json" },
        })
      )
    );
    await expect(api.post("/api/x", {})).rejects.toThrow("low: too big");
  });

  it("throws an ApiError carrying the HTTP status", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "Device 9 not found" }), { status: 404, statusText: "Not Found" })
      )
    );
    const err = await api.get("/api/devices/9").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(404);
    expect((err as ApiError).message).toBe("Device 9 not found");
    expect(isNotFound(err)).toBe(true);
    expect(isNotFound(new Error("Failed to fetch"))).toBe(false);
  });
});

describe("auth handlers", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setAuthHandlers({});
  });

  function fail(status: number, detail: unknown) {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail }), { status, headers: { "content-type": "application/json" } })
      )
    );
  }

  it("calls onUnauthorized for the server's login-required 401", async () => {
    const onUnauthorized = vi.fn();
    setAuthHandlers({ onUnauthorized });
    fail(401, "login required");
    await expect(api.get("/api/devices")).rejects.toMatchObject({ status: 401, code: "login required" });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("does not treat a wrong password as an expired session", async () => {
    const onUnauthorized = vi.fn();
    setAuthHandlers({ onUnauthorized });
    fail(401, "wrong username or password");
    await expect(api.post("/api/auth/login", {})).rejects.toBeInstanceOf(ApiError);
    fail(401, "the current password is wrong");
    await expect(api.put("/api/settings/login", {})).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it("calls onSetupRequired for a 403 setup_required", async () => {
    const onSetupRequired = vi.fn();
    setAuthHandlers({ onSetupRequired });
    fail(403, "setup_required");
    await expect(api.get("/api/devices")).rejects.toMatchObject({ status: 403, code: "setup_required" });
    expect(onSetupRequired).toHaveBeenCalledTimes(1);
  });

  it("leaves other 403s alone", async () => {
    const onSetupRequired = vi.fn();
    setAuthHandlers({ onSetupRequired });
    fail(403, "cross-site request refused");
    await expect(api.post("/api/x", {})).rejects.toBeInstanceOf(ApiError);
    expect(onSetupRequired).not.toHaveBeenCalled();
  });

  it("leaves code undefined when the detail is not a string", async () => {
    fail(422, [{ loc: ["body", "x"], msg: "bad" }]);
    await expect(api.post("/api/x", {})).rejects.toMatchObject({ status: 422, code: undefined });
  });
});
