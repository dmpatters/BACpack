import type { Simulation } from "../api/types";

// Value simulators. Mirrors simulation.py's KINDS_BY_FAMILY and its per-kind
// settings.
export type SimFamily = "analog" | "binary" | "multi-state";

const FAMILY_OF: Record<string, SimFamily> = {
  "analog-input": "analog",
  "analog-output": "analog",
  "analog-value": "analog",
  "binary-input": "binary",
  "binary-output": "binary",
  "binary-value": "binary",
  "multi-state-input": "multi-state",
  "multi-state-output": "multi-state",
  "multi-state-value": "multi-state",
};

export function simFamily(objectType: string): SimFamily | null {
  return FAMILY_OF[objectType] ?? null;
}

/** The family every one of `objectTypes` shares, or null (mixed, or one that can't be simulated). */
export function commonFamily(objectTypes: string[]): SimFamily | null {
  const families = new Set(objectTypes.map(simFamily));
  if (families.size !== 1) return null;
  return [...families][0];
}

export const KINDS: Record<SimFamily, string[]> = {
  analog: ["sine", "triangle", "sawtooth", "square", "random_walk", "noise", "follow"],
  binary: ["square", "random"],
  "multi-state": ["cycle", "random"],
};

export const KIND_LABELS: Record<string, string> = {
  sine: "Sine",
  triangle: "Triangle",
  sawtooth: "Sawtooth (ramp)",
  square: "Square",
  random_walk: "Random walk",
  noise: "Noise",
  follow: "Follow another object",
  random: "Random",
  cycle: "Cycle through states",
};

export const PERIODIC_KINDS = ["sine", "triangle", "sawtooth", "square", "cycle"];

export type SimGroup = "Signal" | "Timing" | "Output";

export interface SimField {
  key: string;
  label: string;
  type: "number" | "source" | "states";
  group: SimGroup;
  optional?: boolean;
  // The label carries the object's unit: a value, not a time or a ratio.
  unit?: boolean;
  help?: string;
  placeholder?: string;
}

const num = (key: string, label: string, group: SimGroup, extra: Partial<SimField> = {}): SimField => ({
  key,
  label,
  type: "number",
  group,
  ...extra,
});
const value = (key: string, label: string, extra: Partial<SimField> = {}) => num(key, label, "Signal", { unit: true, ...extra });
const PHASE = num("phase_s", "Phase offset (s)", "Timing", { optional: true });
const PERIOD = num("period_s", "Period (s)", "Timing");
const LOW_HIGH = [value("low", "Low"), value("high", "High")];
const CENTER_AMPLITUDE = [value("center", "Center"), value("amplitude", "Amplitude ±")];
const ANALOG_COMMON = [
  num("interval_s", "Update every (s)", "Timing"),
  num("decimals", "Decimals", "Output"),
  num("min", "Never below", "Output", { optional: true, unit: true, placeholder: "No limit" }),
  num("max", "Never above", "Output", { optional: true, unit: true, placeholder: "No limit" }),
];
const DWELL = [num("min_dwell_s", "Shortest dwell (s)", "Timing"), num("max_dwell_s", "Longest dwell (s)", "Timing")];
const STATES: SimField = { key: "states", label: "States", type: "states", group: "Signal", optional: true };
const duty = () => num("duty", "Duty (0–1)", "Timing");

const KIND_FIELDS: Record<SimFamily, Record<string, SimField[]>> = {
  analog: {
    sine: [...CENTER_AMPLITUDE, PERIOD, PHASE, ...ANALOG_COMMON],
    triangle: [...LOW_HIGH, PERIOD, PHASE, ...ANALOG_COMMON],
    sawtooth: [...LOW_HIGH, PERIOD, PHASE, ...ANALOG_COMMON],
    square: [...LOW_HIGH, PERIOD, duty(), PHASE, ...ANALOG_COMMON],
    random_walk: [value("start", "Start"), num("max_step", "Max step per update", "Signal"), ...LOW_HIGH, ...ANALOG_COMMON],
    noise: [...CENTER_AMPLITUDE, ...ANALOG_COMMON],
    follow: [
      { key: "source", label: "Source object", type: "source", group: "Signal" },
      num("scale", "Scale", "Signal"),
      value("offset", "Offset"),
      value("noise", "Noise ±", { optional: true, help: "Optional." }),
      ...ANALOG_COMMON,
    ],
  },
  binary: {
    square: [PERIOD, duty(), PHASE],
    random: DWELL,
  },
  "multi-state": {
    cycle: [num("dwell_s", "Hold each state (s)", "Timing"), PHASE, STATES],
    random: [...DWELL, STATES],
  },
};

export function fieldLabel(field: SimField, unit: string): string {
  return field.unit && unit ? `${field.label} (${unit})` : field.label;
}

export function fieldsFor(family: SimFamily, kind: string): SimField[] {
  return KIND_FIELDS[family][kind] ?? [];
}

const DEFAULTS: Record<SimFamily, Record<string, Simulation>> = {
  analog: {
    sine: { kind: "sine", center: 50, amplitude: 10, period_s: 600 },
    triangle: { kind: "triangle", low: 0, high: 100, period_s: 600 },
    sawtooth: { kind: "sawtooth", low: 0, high: 100, period_s: 600 },
    square: { kind: "square", low: 0, high: 100, period_s: 600, duty: 0.5 },
    random_walk: { kind: "random_walk", start: 50, max_step: 1, low: 0, high: 100 },
    noise: { kind: "noise", center: 50, amplitude: 1 },
    follow: { kind: "follow", source: "", scale: 1, offset: 0 },
  },
  binary: {
    square: { kind: "square", period_s: 120, duty: 0.5 },
    random: { kind: "random", min_dwell_s: 30, max_dwell_s: 120 },
  },
  "multi-state": {
    cycle: { kind: "cycle", dwell_s: 60 },
    random: { kind: "random", min_dwell_s: 30, max_dwell_s: 120 },
  },
};

export function defaultSimulation(family: SimFamily, kind: string): Simulation {
  const base = DEFAULTS[family][kind];
  return family === "analog" ? { ...base, interval_s: 5, decimals: 2 } : { ...base };
}

export type SimValues = Record<string, string>;

export function toValues(sim: Simulation): SimValues {
  const values: SimValues = {};
  for (const [key, value] of Object.entries(sim)) {
    if (key === "kind") continue;
    values[key] = Array.isArray(value) ? value.join(", ") : String(value);
  }
  return values;
}

/** The settings to send: only the kind's fields; a blank field is left out
 * (the server fills in an optional one's default, and refuses a required one). */
export function toSimulation(family: SimFamily, kind: string, values: SimValues): Simulation {
  const sim: Simulation = { kind };
  for (const field of fieldsFor(family, kind)) {
    const raw = (values[field.key] ?? "").trim();
    if (raw === "") continue;
    if (field.type === "number") sim[field.key] = Number(raw);
    else if (field.type === "states") sim[field.key] = raw.split(/[\s,;]+/).filter(Boolean).map(Number);
    else sim[field.key] = raw;
  }
  return sim;
}

// A new kind starts from its defaults, keeping each field the old kind had
// too (update interval, decimals, limits, a center...).
export function switchKind(family: SimFamily, from: string, to: string, values: SimValues): SimValues {
  const next = toValues(defaultSimulation(family, to));
  const shared = new Set(fieldsFor(family, from).map((f) => f.key));
  for (const field of fieldsFor(family, to)) {
    if (shared.has(field.key) && values[field.key] !== undefined) next[field.key] = values[field.key];
  }
  return next;
}

// The simulator's own checks (simulation.py's models), as the person types.
export function simulationErrors(family: SimFamily, kind: string, values: SimValues): Record<string, string> {
  const fields = fieldsFor(family, kind);
  const errors: Record<string, string> = {};
  const has = (key: string) => fields.some((f) => f.key === key);
  const raw = (key: string) => (values[key] ?? "").trim();
  const n = (key: string) => (raw(key) === "" ? NaN : Number(raw(key)));
  for (const field of fields) {
    if (field.type !== "number") continue;
    if (raw(field.key) === "" ? !field.optional : !Number.isFinite(n(field.key))) errors[field.key] = "Needs a number.";
  }
  const check = (key: string, bad: boolean, message: string) => {
    if (has(key) && !errors[key] && bad) errors[key] = message;
  };
  check("high", n("low") >= n("high"), "High must be above low.");
  check("max", n("min") > n("max"), "Never above must be at least Never below.");
  check("period_s", n("period_s") <= 0, "Period must be above 0.");
  check("amplitude", n("amplitude") < 0, "Amplitude must be 0 or more.");
  check("start", n("start") < n("low") || n("start") > n("high"), "Start must be between low and high.");
  check("max_step", n("max_step") <= 0, "Step must be above 0.");
  check("min_dwell_s", n("min_dwell_s") < 1, "Dwell must be 1 s or more.");
  check("max_dwell_s", n("max_dwell_s") < 1, "Dwell must be 1 s or more.");
  check("max_dwell_s", n("min_dwell_s") > n("max_dwell_s"), "Longest dwell can't be below shortest.");
  check("dwell_s", n("dwell_s") < 1, "Hold time must be 1 s or more.");
  check("interval_s", n("interval_s") < 1, "Update interval must be 1 s or more.");
  check(
    "decimals",
    !(Number.isInteger(n("decimals")) && n("decimals") >= 0 && n("decimals") <= 6),
    "Decimals must be a whole number from 0 to 6."
  );
  check("duty", n("duty") < 0 || n("duty") > 1, "Duty must be from 0 to 1.");
  check("phase_s", n("phase_s") < 0, "Phase offset must be 0 or more.");
  if (has("source") && raw("source") === "") errors.source = "Choose a source object.";
  if (has("states") && raw("states").split(/[\s,;]+/).filter(Boolean).length === 1) errors.states = "Check at least two states.";
  return errors;
}

function duration(seconds: number): string {
  if (seconds < 120) return `${+seconds.toFixed(1)} s`;
  if (seconds < 7200) return `${+(seconds / 60).toFixed(1)} min`;
  return `${+(seconds / 3600).toFixed(1)} h`;
}

const fmt = (x: number) => String(+x.toFixed(6));

/** A one-line description, e.g. "swings 69 – 75 every 10 min"; "" while a needed field is blank. */
export function describeSimulation(family: SimFamily, sim: Simulation): string {
  const n = (key: string): number => (typeof sim[key] === "number" ? (sim[key] as number) : NaN);
  const need = (...keys: string[]) => keys.every((key) => Number.isFinite(n(key)));
  const states = Array.isArray(sim.states) && sim.states.length ? `states ${sim.states.join(", ")}` : null;
  switch (`${family}:${sim.kind}`) {
    case "analog:sine":
      return need("center", "amplitude", "period_s")
        ? `swings ${fmt(n("center") - n("amplitude"))} – ${fmt(n("center") + n("amplitude"))} every ${duration(n("period_s"))}`
        : "";
    case "analog:triangle":
      return need("low", "high", "period_s") ? `ramps ${fmt(n("low"))} – ${fmt(n("high"))} and back every ${duration(n("period_s"))}` : "";
    case "analog:sawtooth":
      return need("low", "high", "period_s")
        ? `ramps ${fmt(n("low"))} → ${fmt(n("high"))}, then drops back, every ${duration(n("period_s"))}`
        : "";
    case "analog:square": {
      const duty = Number.isFinite(n("duty")) ? n("duty") : 0.5;
      return need("low", "high", "period_s")
        ? `${fmt(n("high"))} for ${duration(n("period_s") * duty)} of every ${duration(n("period_s"))}, otherwise ${fmt(n("low"))}`
        : "";
    }
    case "analog:random_walk":
      return need("low", "high", "max_step") ? `wanders ${fmt(n("low"))} – ${fmt(n("high"))}, up to ±${fmt(n("max_step"))} per update` : "";
    case "analog:noise":
      return need("center", "amplitude") ? `random ${fmt(n("center") - n("amplitude"))} – ${fmt(n("center") + n("amplitude"))}` : "";
    case "analog:follow":
      return typeof sim.source === "string" && sim.source
        ? `follows ${sim.source} · scale ${fmt(Number.isFinite(n("scale")) ? n("scale") : 1)} · offset ${fmt(Number.isFinite(n("offset")) ? n("offset") : 0)}`
        : "";
    case "binary:square": {
      const duty = Number.isFinite(n("duty")) ? n("duty") : 0.5;
      return need("period_s") ? `active ${duration(n("period_s") * duty)} of every ${duration(n("period_s"))}` : "";
    }
    case "binary:random":
      return need("min_dwell_s", "max_dwell_s") ? `flips after ${fmt(n("min_dwell_s"))}–${fmt(n("max_dwell_s"))} s` : "";
    case "multi-state:cycle":
      return need("dwell_s") ? `steps through ${states ?? "every state"}, ${duration(n("dwell_s"))} each` : "";
    case "multi-state:random":
      return need("min_dwell_s", "max_dwell_s")
        ? `a random ${states ? `one of ${states}` : "state"} every ${fmt(n("min_dwell_s"))}–${fmt(n("max_dwell_s"))} s`
        : "";
    default:
      return "";
  }
}
