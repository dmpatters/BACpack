import { describe, expect, it } from "vitest";
import {
  bacnetWritableHelp,
  canBeScheduleWriteTarget,
  hasRelinquishDefault,
  hasUnits,
  isAlarmingCapable,
  valueFamily,
  valueProblem,
} from "./objectTypes";

describe("hasUnits", () => {
  it("is true for analog types and an analog schedule only", () => {
    expect(hasUnits("analog-input")).toBe(true);
    expect(hasUnits("schedule", "analog")).toBe(true);
    expect(hasUnits("schedule", "binary")).toBe(false);
    expect(hasUnits("binary-value")).toBe(false);
    expect(hasUnits("calendar")).toBe(false);
  });
});

describe("hasRelinquishDefault", () => {
  it("is true for types with a priority array only", () => {
    for (const t of [
      "analog-output", "analog-value", "binary-output", "binary-value", "multi-state-output", "multi-state-value",
    ]) {
      expect(hasRelinquishDefault(t)).toBe(true);
    }
    for (const t of ["analog-input", "binary-input", "multi-state-input", "schedule", "calendar", "notification-class"]) {
      expect(hasRelinquishDefault(t)).toBe(false);
    }
  });
});

describe("bacnetWritableHelp", () => {
  it("says a point's flag covers Present_Value", () => {
    for (const t of ["analog-value", "multi-state-value", "binary-output"]) {
      expect(bacnetWritableHelp(t)).toBe("Unchecked, BACnet clients can't command Present_Value.");
    }
  });

  it("says a schedule's or calendar's flag lets a BACnet client configure it", () => {
    expect(bacnetWritableHelp("schedule")).toBe("Lets a BACnet client configure this schedule.");
    expect(bacnetWritableHelp("calendar")).toBe("Lets a BACnet client configure this calendar.");
  });

  it("says the flag does nothing on other types", () => {
    for (const t of ["analog-input", "binary-input", "multi-state-input", "notification-class", "trend-log"]) {
      expect(bacnetWritableHelp(t)).toBe("No effect on this object type.");
    }
  });
});

describe("canBeScheduleWriteTarget", () => {
  it("refuses types with no writable Present_Value", () => {
    for (const t of ["device", "file", "network-port", "notification-class", "trend-log", "schedule", "calendar"]) {
      expect(canBeScheduleWriteTarget(t)).toBe(false);
    }
  });

  it("offers everything else, including inputs and unknown remote types", () => {
    for (const t of ["analog-value", "binary-output", "analog-input", "loop", "integer-value", "proprietary-600"]) {
      expect(canBeScheduleWriteTarget(t)).toBe(true);
    }
  });
});

describe("isAlarmingCapable", () => {
  it("covers the 7 intrinsic-reporting types", () => {
    for (const t of [
      "analog-input",
      "analog-output",
      "analog-value",
      "binary-input",
      "binary-value",
      "multi-state-input",
      "multi-state-value",
    ]) {
      expect(isAlarmingCapable(t)).toBe(true);
    }
  });

  it("excludes the types with no intrinsic reporting", () => {
    for (const t of ["binary-output", "multi-state-output", "schedule", "calendar", "notification-class"]) {
      expect(isAlarmingCapable(t)).toBe(false);
    }
  });
});

describe("valueFamily", () => {
  it("names the value family of a point type", () => {
    expect(valueFamily("analog-output")).toBe("analog");
    expect(valueFamily("binary-value")).toBe("binary");
    expect(valueFamily("multi-state-input")).toBe("multi-state");
    expect(valueFamily("calendar")).toBeNull();
  });
});

describe("valueProblem", () => {
  it("says why a value doesn't fit its family", () => {
    expect(valueProblem("72", "binary", 0)).toBe("72 is not a binary value");
    expect(valueProblem("active", "binary", 0)).toBeNull();
    expect(valueProblem("active", "analog", 0)).toBe("active is not a number");
    expect(valueProblem("-1.5", "analog", 0)).toBeNull();
    expect(valueProblem("3", "multi-state", 2)).toBe("3 is not a state");
    expect(valueProblem("1.5", "multi-state", 2)).toBe("1.5 is not a state");
    expect(valueProblem("2", "multi-state", 2)).toBeNull();
    expect(valueProblem("", "binary", 0)).toBeNull();
  });
});
