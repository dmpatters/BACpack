import { describe, expect, it } from "vitest";
import { credentialsFollow, urlClearsCredentials } from "./credentialsFollow";

describe("credentialsFollow", () => {
  it.each([
    ["http://a.invalid/x", "http://a.invalid/y?q=1"],
    ["http://A.invalid/x", "http://a.INVALID/x"],
    ["http://a.invalid", "http://a.invalid:80/x"],
    ["http://a.invalid", "https://a.invalid"],
    ["http://a.invalid:80/x", "https://a.invalid:443/x"],
  ])("%s -> %s keeps them", (a, b) => expect(credentialsFollow(a, b)).toBe(true));

  it.each([
    ["https://a.invalid", "http://a.invalid"],
    ["http://a.invalid", "https://a.invalid:8443"],
    ["http://a.invalid:8080", "https://a.invalid"],
    ["http://a.invalid", "http://b.invalid"],
    ["http://a.invalid", "http://a.invalid:81"],
    ["", "http://a.invalid"],
    ["not a url", "http://a.invalid"],
  ])("%s -> %s drops them", (a, b) => expect(credentialsFollow(a, b)).toBe(false));
});

describe("urlClearsCredentials", () => {
  it("is true for another host", () => expect(urlClearsCredentials("http://a.invalid", "http://b.invalid")).toBe(true));
  it("is false while the new URL isn't a URL yet", () => {
    expect(urlClearsCredentials("http://a.invalid", "")).toBe(false);
    expect(urlClearsCredentials("http://a.invalid", "http://")).toBe(false);
    expect(urlClearsCredentials("http://a.invalid", "ftp://b.invalid")).toBe(false);
  });
  it("is false for an unchanged URL", () => expect(urlClearsCredentials("http://a.invalid/x", "http://a.invalid/x")).toBe(false));
});
