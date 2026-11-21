import { describe, expect, it } from "vitest";
import { BIND_ADDRESS_HELP, bindParts, formatBind, parseBind, sameNetwork } from "./bind";

describe("BIND_ADDRESS_HELP", () => {
  it("gives the format with an example", () => {
    expect(BIND_ADDRESS_HELP).toContain("optional port");
    expect(parseBind(BIND_ADDRESS_HELP.match(/e\.g\. (\S+)\.$/)![1])).not.toBeNull();
    // What the backend accepts: an address alone, with a prefix, or with a port.
    expect(parseBind("192.168.1.50")).not.toBeNull();
    expect(parseBind("192.168.1.50/24:47810")).not.toBeNull();
  });
});

describe("formatBind", () => {
  it("shows the default port explicitly", () => {
    expect(formatBind("192.168.1.140/24")).toBe("192.168.1.140/24 · UDP 47808");
  });

  it("shows an explicit port", () => {
    expect(formatBind("192.168.1.140/24:47809")).toBe("192.168.1.140/24 · UDP 47809");
  });

  it("copes with no prefix", () => {
    expect(formatBind("192.168.1.140")).toBe("192.168.1.140 · UDP 47808");
    expect(formatBind("192.168.1.140:47810")).toBe("192.168.1.140 · UDP 47810");
  });

  it("says default with no address and no fallback", () => {
    expect(formatBind(null)).toBe("default");
  });

  it("shows the server default, marked, for a device with no address of its own", () => {
    expect(formatBind(null, "192.168.1.140/24")).toBe("192.168.1.140/24 · UDP 47808 (default)");
    expect(formatBind(null, "192.168.1.140/24:47811")).toBe("192.168.1.140/24 · UDP 47811 (default)");
  });

  it("leaves a text it can't read as it is", () => {
    expect(formatBind("not-an-address")).toBe("not-an-address");
  });
});

describe("parseBind", () => {
  it("splits host, prefix and port", () => {
    expect(parseBind("192.168.1.140/24:47809")).toEqual({ host: "192.168.1.140", prefix: 24, port: 47809 });
    expect(parseBind("10.0.0.5")).toEqual({ host: "10.0.0.5", prefix: null, port: 47808 });
    expect(parseBind("nope")).toBeNull();
  });
});

describe("sameNetwork", () => {
  it.each([
    ["192.168.1.200/24", "192.168.1.221/24", true],
    ["192.168.1.200/24:47810", "192.168.1.221/24:47810", true],
    ["192.168.1.200/24", "192.168.1.200/24:47810", false],
    ["192.168.56.1/24", "192.168.1.200/24", false],
    ["192.168.1.200/24", "192.168.1.221", true],
    ["192.168.1.200", "192.168.1.221", false],
  ])("%s and %s: %s", (a, b, expected) => {
    expect(sameNetwork(a, b)).toBe(expected);
  });

  it("can't tell for a device on the server default", () => {
    expect(sameNetwork(null, "192.168.1.200/24")).toBe(false);
    expect(sameNetwork("192.168.1.200/24", null)).toBe(false);
  });
});

describe("bindParts", () => {
  it("splits an address and its port, 47808 when none is written", () => {
    expect(bindParts("192.168.1.200/24:47809")).toEqual({ address: "192.168.1.200/24", port: "47809" });
    expect(bindParts("192.168.1.200/24")).toEqual({ address: "192.168.1.200/24", port: "47808" });
  });

  it("marks the server default and says default with neither", () => {
    expect(bindParts(null, "192.168.1.140/24")).toEqual({ address: "192.168.1.140/24 (default)", port: "47808" });
    expect(bindParts(null)).toEqual({ address: "default", port: "—" });
  });

  it("shows an address it can't read as written", () => {
    expect(bindParts("not-an-address")).toEqual({ address: "not-an-address", port: "—" });
  });
});
