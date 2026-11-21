import { describe, expect, it } from "vitest";
import {
  KINDS,
  commonFamily,
  defaultSimulation,
  describeSimulation,
  fieldLabel,
  fieldsFor,
  simFamily,
  simulationErrors,
  switchKind,
  toSimulation,
  toValues,
} from "./simulation";

describe("simulation utils", () => {
  it("maps object types to families", () => {
    expect(simFamily("analog-input")).toBe("analog");
    expect(simFamily("binary-output")).toBe("binary");
    expect(simFamily("multi-state-value")).toBe("multi-state");
    expect(simFamily("schedule")).toBeNull();
    expect(KINDS.binary).toEqual(["square", "random"]);
  });

  it("finds the family every selected object shares", () => {
    expect(commonFamily(["analog-input", "analog-value"])).toBe("analog");
    expect(commonFamily(["analog-input", "binary-value"])).toBeNull();
    expect(commonFamily(["analog-value", "schedule"])).toBeNull();
    expect(commonFamily([])).toBeNull();
  });

  it("round-trips settings through form values, dropping blank optional fields", () => {
    const sim = { ...defaultSimulation("analog", "sine"), phase_s: 0 };
    const values = toValues(sim);
    expect(values.center).toBe("50");
    expect(toSimulation("analog", "sine", { ...values, min: "" })).toEqual({
      kind: "sine",
      center: 50,
      amplitude: 10,
      period_s: 600,
      phase_s: 0,
      interval_s: 5,
      decimals: 2,
    });
    expect(
      toSimulation("multi-state", "cycle", { dwell_s: "30", states: "1, 3;4" })
    ).toEqual({ kind: "cycle", dwell_s: 30, states: [1, 3, 4] });
    expect(toSimulation("analog", "follow", { source: "analog-value:3", scale: "2", offset: "1" })).toEqual({
      kind: "follow",
      source: "analog-value:3",
      scale: 2,
      offset: 1,
    });
  });

  it("describes a simulation in one line", () => {
    expect(describeSimulation("analog", { kind: "sine", center: 72, amplitude: 3, period_s: 600 })).toBe(
      "swings 69 – 75 every 10 min"
    );
    expect(describeSimulation("binary", { kind: "square", period_s: 120, duty: 0.25 })).toBe(
      "active 30 s of every 2 min"
    );
    expect(describeSimulation("multi-state", { kind: "cycle", dwell_s: 60, states: [1, 3] })).toBe(
      "steps through states 1, 3, 60 s each"
    );
    expect(describeSimulation("analog", { kind: "sine", center: 72 })).toBe("");
  });
});

describe("simulation field groups", () => {
  it("groups a sine's fields into Signal, Timing and Output", () => {
    const byGroup = (group: string) => fieldsFor("analog", "sine").filter((f) => f.group === group).map((f) => f.key);
    expect(byGroup("Signal")).toEqual(["center", "amplitude"]);
    expect(byGroup("Timing")).toEqual(["period_s", "phase_s", "interval_s"]);
    expect(byGroup("Output")).toEqual(["decimals", "min", "max"]);
  });

  it("gives binary and multi-state kinds no Output group", () => {
    expect(fieldsFor("binary", "square").some((f) => f.group === "Output")).toBe(false);
    expect(fieldsFor("multi-state", "cycle").map((f) => f.key)).toEqual(["dwell_s", "phase_s", "states"]);
  });

  it("puts the unit in the labels of values, not of times", () => {
    const [center] = fieldsFor("analog", "sine");
    expect(fieldLabel(center, "°F")).toBe("Center (°F)");
    expect(fieldLabel(center, "")).toBe("Center");
    const period = fieldsFor("analog", "sine").find((f) => f.key === "period_s")!;
    expect(fieldLabel(period, "°F")).toBe("Period (s)");
  });
});

describe("switchKind", () => {
  it("keeps what the kinds share and takes the new kind's defaults for the rest", () => {
    const sine = { ...toValues(defaultSimulation("analog", "sine")), interval_s: "10", center: "60" };
    const noise = switchKind("analog", "sine", "noise", sine);
    expect(noise).toMatchObject({ interval_s: "10", center: "60", amplitude: "10", decimals: "2" });
    expect(noise).not.toHaveProperty("period_s");
  });

  it("drops a field the new kind doesn't have", () => {
    const square = { ...toValues(defaultSimulation("binary", "square")), phase_s: "30" };
    expect(switchKind("binary", "square", "random", square)).toEqual(toValues(defaultSimulation("binary", "random")));
  });
});

describe("simulationErrors", () => {
  const values = (kind: string, family: "analog" | "binary" | "multi-state" = "analog") =>
    toValues(defaultSimulation(family, kind));

  it("is quiet for the defaults", () => {
    expect(simulationErrors("analog", "sine", values("sine"))).toEqual({});
    expect(simulationErrors("multi-state", "random", values("random", "multi-state"))).toEqual({});
  });

  it.each([
    ["triangle", { high: "-5" }, "high", "High must be above low."],
    ["sine", { max: "10", min: "20" }, "max", "Never above must be at least Never below."],
    ["sine", { period_s: "0" }, "period_s", "Period must be above 0."],
    ["sine", { amplitude: "-1" }, "amplitude", "Amplitude must be 0 or more."],
    ["random_walk", { start: "200" }, "start", "Start must be between low and high."],
    ["random_walk", { max_step: "0" }, "max_step", "Step must be above 0."],
    ["sine", { interval_s: "0.5" }, "interval_s", "Update interval must be 1 s or more."],
    ["sine", { decimals: "1.5" }, "decimals", "Decimals must be a whole number from 0 to 6."],
    ["square", { duty: "2" }, "duty", "Duty must be from 0 to 1."],
    ["sine", { phase_s: "-3" }, "phase_s", "Phase offset must be 0 or more."],
    ["sine", { center: "" }, "center", "Needs a number."],
    ["follow", { source: "" }, "source", "Choose a source object."],
  ])("%s with %o flags %s", (kind, patch, key, message) => {
    expect(simulationErrors("analog", kind, { ...values(kind), ...patch })[key]).toBe(message);
  });

  it("checks dwell times and states", () => {
    const random = values("random", "multi-state");
    expect(simulationErrors("multi-state", "random", { ...random, min_dwell_s: "0" }).min_dwell_s).toBe("Dwell must be 1 s or more.");
    expect(simulationErrors("multi-state", "random", { ...random, max_dwell_s: "10" }).max_dwell_s).toBe(
      "Longest dwell can't be below shortest."
    );
    expect(simulationErrors("multi-state", "random", { ...random, states: "2" }).states).toBe("Check at least two states.");
    expect(simulationErrors("multi-state", "cycle", { ...values("cycle", "multi-state"), dwell_s: "0" }).dwell_s).toBe(
      "Hold time must be 1 s or more."
    );
  });

  it("leaves an optional field blank", () => {
    expect(simulationErrors("analog", "sine", { ...values("sine"), min: "", max: "" })).toEqual({});
  });
});

describe("describeSimulation follow", () => {
  it("names the source and its settings, not a formula", () => {
    expect(describeSimulation("analog", { kind: "follow", source: "analog-value:3", scale: 1.8, offset: 32 })).toBe(
      "follows analog-value:3 · scale 1.8 · offset 32"
    );
  });
});
