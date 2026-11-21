import { describe, expect, it } from "vitest";
import { controlledFrom, PRIORITY_NAMES } from "./priorities";

describe("controlledFrom", () => {
  it("names the controlling priority", () => {
    expect(controlledFrom({ priority_array: Array(16).fill(null), controlling_priority: 8 })).toBe("from priority 8");
  });

  it("says Relinquish default when every slot is empty", () => {
    expect(controlledFrom({ priority_array: Array(16).fill(null), controlling_priority: null })).toBe(
      "from Relinquish default"
    );
  });

  it("is null for an object with no priority array", () => {
    expect(controlledFrom({ priority_array: null, controlling_priority: null })).toBeNull();
  });
});

describe("PRIORITY_NAMES", () => {
  it("carries the named priorities", () => {
    expect(PRIORITY_NAMES[6]).toBe("Minimum on/off");
    expect(PRIORITY_NAMES[1]).toBe("Manual life safety");
    expect(PRIORITY_NAMES[3]).toBeUndefined();
  });
});
