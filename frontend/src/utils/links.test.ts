import { describe, expect, it } from "vitest";
import { REPO_URL, featureRequestUrl } from "./links";

describe("featureRequestUrl", () => {
  it("names types, counts and vendor, never object names", () => {
    const url = featureRequestUrl({ accumulator: 2, loop: 1 }, "Acme Controls", "0.38.0");
    expect(url.startsWith(`${REPO_URL}/issues/new?`)).toBe(true);
    const body = new URL(url).searchParams.get("body")!;
    expect(body).toContain("accumulator: 2");
    expect(body).toContain("loop: 1");
    expect(body).toContain("Acme Controls");
    expect(body).toContain("0.38.0");
    expect(url).not.toContain("AHU-1");
  });

  it("encodes the title and body", () => {
    const url = featureRequestUrl({ "trend-log-multiple": 1 }, "A & B / C?", "0.38.0");
    const params = new URL(url).searchParams;
    expect(params.get("title")).toBe("Support for trend-log-multiple objects in clones");
    expect(params.get("body")).toContain("A & B / C?");
    expect(url).not.toContain(" ");
  });

  it("says the types came from an EDE file when they did", () => {
    const params = new URL(featureRequestUrl({ "event-enrollment (9)": 1 }, null, "0.39.0", "ede")).searchParams;
    expect(params.get("title")).toBe("Support for event-enrollment (9) objects in EDE imports");
    expect(params.get("body")).toContain("Importing an EDE file skipped these object types");
  });

  it("works without a vendor name", () => {
    const body = new URL(featureRequestUrl({ loop: 1 }, null, "0.38.0")).searchParams.get("body")!;
    expect(body).toContain("Vendor: not given");
  });
});
