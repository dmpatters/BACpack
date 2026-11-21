import { describe, expect, it } from "vitest";
import { dccText, objectId, plural, segmentationText, words } from "./text";

describe("text helpers", () => {
  it("writes real plurals", () => {
    expect(plural(1, "object")).toBe("1 object");
    expect(plural(0, "object")).toBe("0 objects");
    expect(plural(2, "entry", "entries")).toBe("2 entries");
  });
  it("writes object ids with a colon", () => {
    expect(objectId("analog-value", 0)).toBe("analog-value:0");
  });
  it("turns kebab-case into words", () => {
    expect(words("read-property-multiple")).toBe("Read property multiple");
    expect(words("")).toBe("");
  });
  it("keeps acronyms and multi-state whole", () => {
    expect(words("subscribe-cov")).toBe("Subscribe COV");
    expect(words("utc-time-synchronization")).toBe("UTC time synchronization");
    expect(words("confirmed-cov-notification")).toBe("Confirmed COV notification");
    expect(words("multi-state-input")).toBe("Multi-state input");
  });
  it("says protocol state in words", () => {
    expect(dccText("enable")).toBe("Communication enabled");
    expect(dccText("disable")).toBe("Communication disabled");
    expect(dccText("disableInitiation")).toBe("Initiation disabled");
  });
  it("names segmentation", () => {
    expect(segmentationText("segmented-both")).toBe("Both directions");
    expect(segmentationText("no-segmentation")).toBe("None");
    expect(segmentationText("segmented-sideways")).toBe("Segmented sideways");
  });
});
