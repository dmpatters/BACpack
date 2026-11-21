import { describe, expect, it } from "vitest";
import type { LiveObjectDTO } from "../api/types";
import { mappingValue } from "./mappingValue";

const LIVE: LiveObjectDTO = {
  object_id: 7,
  object_type: "analog-value",
  instance: 1,
  present_value: 99,
  priority_array: [...Array(7).fill(null), 99, null, 21.4, ...Array(6).fill(null)],
  controlling_priority: 8,
  relinquish_default: null,
  status_flags: ["overridden"],
  event_state: "normal",
  reliability: null,
  out_of_service: false,
};

describe("mappingValue", () => {
  it("reads the feed slot, so an override doesn't hide what the connector wrote", () => {
    expect(mappingValue(LIVE, "read")).toBe(21.4);
  });

  it("reads a proxy's present value, and that of an object without a priority array", () => {
    expect(mappingValue(LIVE, "proxy")).toBe(99);
    expect(mappingValue({ ...LIVE, priority_array: null }, "read")).toBe(99);
  });

  it("has nothing without a live answer", () => {
    expect(mappingValue(undefined, "read")).toBeUndefined();
  });
});
