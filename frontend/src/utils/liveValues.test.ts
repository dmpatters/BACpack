import { describe, expect, it } from "vitest";
import { eventTone, flagTone, formatLiveValue } from "./liveValues";

const analog = { object_type: "analog-value", state_text: null, schedule_value_type: null };
const binary = { object_type: "binary-value", state_text: null, schedule_value_type: null };
const multi = { object_type: "multi-state-value", state_text: ["Off", "Low", "High"], schedule_value_type: null };

describe("formatLiveValue", () => {
  it("rounds analog values to two decimals and drops trailing zeros", () => {
    expect(formatLiveValue(72.456, analog)).toBe("72.46");
    expect(formatLiveValue(70, analog)).toBe("70");
  });

  it("names binary values", () => {
    expect(formatLiveValue("active", binary)).toBe("Active");
    expect(formatLiveValue("inactive", binary)).toBe("Inactive");
    expect(formatLiveValue("active", { object_type: "schedule", state_text: null, schedule_value_type: "binary" })).toBe("Active");
  });

  it("names multi-state values by number and name", () => {
    expect(formatLiveValue(2, multi)).toBe("2 · Low");
    expect(formatLiveValue(7, multi)).toBe("7 · not a state");
    expect(formatLiveValue(2, { ...multi, state_text: ["Off", " ", "High"] })).toBe("2 · State 2");
  });

  it("names a multi-state schedule's values by number when it has no state names", () => {
    const schedule = { object_type: "schedule", state_text: null, schedule_value_type: "multi-state" as const };
    expect(formatLiveValue(2, schedule)).toBe("2 · State 2");
    expect(formatLiveValue(2, { ...schedule, state_text: [] })).toBe("2 · State 2");
  });

  it("reads a calendar's value as words", () => {
    const calendar = { object_type: "calendar", state_text: null, schedule_value_type: null };
    expect(formatLiveValue(true, calendar)).toBe("In calendar");
    expect(formatLiveValue(false, calendar)).toBe("Not in calendar");
  });

  it("shows nothing as an em dash", () => {
    expect(formatLiveValue(null, analog)).toBe("—");
    expect(formatLiveValue(undefined, analog)).toBe("—");
  });
});

describe("tones", () => {
  it("colours flags and event states by how bad they are", () => {
    expect(flagTone("in-alarm")).toBe("danger");
    expect(flagTone("fault")).toBe("danger");
    expect(flagTone("overridden")).toBe("warn");
    expect(flagTone("out-of-service")).toBeUndefined();
    expect(eventTone("normal")).toBe("ok");
    expect(eventTone("high-limit")).toBe("danger");
  });
});
