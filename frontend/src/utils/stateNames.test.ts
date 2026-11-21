import { describe, expect, it } from "vitest";
import type { BacnetObjectDTO } from "../api/types";
import {
  addState,
  deleteState,
  moveState,
  presetStates,
  referenceText,
  renameState,
  renumberValue,
  stateCountText,
  stateOptionLabel,
  stateProblems,
  stateReferences,
  trackOrigins,
} from "./stateNames";

const obj = (overrides: Partial<BacnetObjectDTO>): BacnetObjectDTO =>
  ({
    id: 1,
    device_id: 599002,
    object_type: "multi-state-value",
    instance_number: 3,
    name: "MSV-3",
    alarm_values: null,
    simulation: null,
    ...overrides,
  }) as BacnetObjectDTO;

describe("state list edits", () => {
  it("renames without renumbering", () => {
    expect(renameState(["Off", "On"], 1, "Run")).toEqual({ names: ["Off", "Run"], renumber: [1, 2] });
  });

  it("adds a named state at the end", () => {
    expect(addState(["Off", "On"])).toEqual({ names: ["Off", "On", "State 3"], renumber: [1, 2] });
  });

  it("swaps a moved state with its neighbour and reports both numbers", () => {
    expect(moveState(["Off", "Low", "High"], 0, 1)).toEqual({ names: ["Low", "Off", "High"], renumber: [2, 1, 3] });
    expect(moveState(["Off", "Low"], 0, -1)).toEqual({ names: ["Off", "Low"], renumber: [1, 2] });
  });

  it("deletes a state and moves the later ones up", () => {
    expect(deleteState(["Off", "Low", "High"], 1)).toEqual({ names: ["Off", "High"], renumber: [1, null, 2] });
  });

  it("fills from a preset, keeping the numbers the preset still has", () => {
    expect(presetStates(["A", "B", "C", "D"], ["Off", "On"])).toEqual({ names: ["Off", "On"], renumber: [1, 2, null, null] });
  });

  it("renumbers a stored state number and leaves other text alone", () => {
    const { renumber } = moveState(["Off", "Low", "High"], 0, 1);
    expect(renumberValue("1", renumber)).toBe("2");
    expect(renumberValue("3", renumber)).toBe("3");
    expect(renumberValue("2", deleteState(["Off", "Low"], 1).renumber)).toBe("");
    expect(renumberValue("", renumber)).toBe("");
    expect(renumberValue("9", renumber)).toBe("9");
  });

  it("tracks which saved state each row was through several edits", () => {
    let origins: (number | null)[] = [1, 2, 3];
    const first = moveState(["Off", "Low", "High"], 1, -1);
    origins = trackOrigins(origins, first);
    expect(origins).toEqual([2, 1, 3]);
    const second = addState(first.names);
    origins = trackOrigins(origins, second);
    expect(origins).toEqual([2, 1, 3, null]);
    origins = trackOrigins(origins, deleteState(second.names, 0));
    expect(origins).toEqual([1, 3, null]);
  });
});

describe("state wording", () => {
  it("says the count in words", () => {
    expect(stateCountText(1)).toBe("1 state");
    expect(stateCountText(4)).toBe("4 states");
  });

  it("labels an option with its number and name", () => {
    expect(stateOptionLabel(2, "Low")).toBe("2 · Low");
    expect(stateOptionLabel(2, " ")).toBe("2 · (no name)");
  });
});

describe("stateProblems", () => {
  it("finds blank and repeated names, ignoring case and spaces", () => {
    const problems = stateProblems(["Off", "", "off "]);
    expect(problems.rows.get(1)).toBe("State 2 needs a name.");
    expect(problems.rows.get(2)).toBe("State 3 repeats the name of state 1.");
    expect(problems.rows.has(0)).toBe(false);
    expect(problems.message).toBe("State 2 needs a name.");
  });

  it("needs at least one state", () => {
    expect(stateProblems([]).message).toBe("Add at least one state.");
  });

  it("is quiet for good names", () => {
    expect(stateProblems(["Off", "On"])).toEqual({ message: null, rows: new Map() });
  });
});

describe("stateReferences", () => {
  const schedule = (overrides: Partial<BacnetObjectDTO>) =>
    obj({
      id: 9,
      object_type: "schedule",
      instance_number: 1,
      name: "OCC_SCHEDULE",
      schedule_value_type: "multi-state",
      schedule_default: "1",
      weekly_schedule: [[{ time: "08:00", value: "3" }, { time: "17:00", value: null }], [], [], [], [], [], []],
      exception_schedule: [{ period: { kind: "date" }, priority: 1, time_values: [{ time: "00:00", value: "2" }] }],
      schedule_write_targets: [{ object_type: "multi-state-value", instance_number: 3, property_identifier: "present-value" }],
      ...overrides,
    });

  it("lists a local multi-state schedule writing this object, with every state it uses", () => {
    expect(stateReferences(obj({}), [schedule({})])).toEqual([{ who: "Schedule OCC_SCHEDULE", states: [1, 2, 3] }]);
  });

  it("skips schedules writing another object, another device, or another value type", () => {
    const other = schedule({ schedule_write_targets: [{ object_type: "multi-state-value", instance_number: 4, property_identifier: "present-value" }] });
    const remote = schedule({
      schedule_write_targets: [{ device_instance: 1234, object_type: "multi-state-value", instance_number: 3, property_identifier: "present-value" }],
    });
    const analog = schedule({ schedule_value_type: "analog" });
    expect(stateReferences(obj({}), [other, remote, analog])).toEqual([]);
  });

  it("lists this object's own alarm and simulation states", () => {
    const self = obj({ alarm_values: [3, 2, 3], simulation: { kind: "cycle", dwell_s: 60, states: [1, 4] } });
    expect(stateReferences(self, [])).toEqual([
      { who: "Alarming on this object", states: [2, 3] },
      { who: "Simulation on this object", states: [1, 4] },
    ]);
  });

  it("names the states a reference uses", () => {
    const names = ["Off", "Low", "Medium"];
    expect(referenceText({ who: "Schedule OCC", states: [3] }, names)).toBe("Schedule OCC uses state 3 (Medium).");
    expect(referenceText({ who: "Schedule OCC", states: [1, 2, 5] }, names)).toBe("Schedule OCC uses states 1 (Off), 2 (Low) and 5.");
  });
});
