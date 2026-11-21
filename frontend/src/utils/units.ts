import type { ComboOption } from "../components/Combobox";

// The handful a BAS integrator reaches for first, shown in this order above
// the groups (and only there).
export const COMMON_UNITS = [
  "degreesFahrenheit",
  "degreesCelsius",
  "percent",
  "percentRelativeHumidity",
  "partsPerMillion",
  "inchesOfWater",
  "poundsForcePerSquareInch",
  "cubicFeetPerMinute",
  "litersPerSecond",
  "kilowatts",
  "kilowattHours",
  "amperes",
  "volts",
  "noUnits",
];

// First match wins, so the order matters: Flow before Volume and Length,
// Electrical before Energy (voltAmpereHours), Light before Length
// (candelasPerSquareMeter). Anything unmatched lands in Other.
const GROUP_RULES: Array<[string, RegExp]> = [
  ["Temperature", /^(degrees(Celsius|Fahrenheit|Kelvin)|deltaDegrees|degreeDays)/],
  ["Humidity and air quality", /^(percentRelativeHumidity|partsPer|gramsOfWater|(micro|milli|nano)gramsPerCubicMeter|percentObscuration)/],
  ["Pressure", /^((hecto|kilo)?pascals|(milli)?bars|poundsForcePerSquareInch|psiPerDegreeFahrenheit|[a-z]+Of(Water|Mercury))$/],
  ["Flow", /^(cubic(Feet|Meters)|(milli)?liters|(us|imperial)Gallons|(milli|kilo)?grams|poundsMass|tons)Per(Second|Minute|Hour|Day)$/],
  ["Electrical", /^(milli)?amperes|[aA]mpere|^(kilo|mega|milli)?volts|[oO]hm|farads|henrys|[sS]iemens|webers|teslas|powerFactor|decibels(Volt|Millivolt)/],
  ["Power", /^(milli|kilo|mega)?watts$|^watts|horsepower|tonsRefrigeration|[bB]tusPerHour$/],
  ["Energy", /Hours|joules|Joules|^(kilo|mega)?[bB]tus|therms/],
  ["Volume", /^(cubic(Feet|Meters)|(milli)?liters|(us|imperial)Gallons)$/],
  ["Mass", /^((milli|kilo)?grams|poundsMass|tons)$/],
  ["Concentration and water quality", /PerCubic(Centimeter|Meter)$|PerLiter$|PerMilliliter$|^pH$|nephelometric/],
  ["Light", /lumens|luxes|candelas|footCandles/],
  ["Force and torque", /^newton/],
  ["Length, area and speed", /meters|Meters|feet|Feet|inches|Inches|miles|kilometers|square|Square/],
  ["Time", /^(milli|hundredths)?[sS]econds$|^(minutes|hours|days|weeks|months|years)$/],
  ["Frequency and rotation", /hertz|^per(Second|Minute|Hour)$|^cyclesPer|revolutionsPerMinute|radians|degreesAngular|degreesPhase/],
  ["Percent and ratio", /^percent|perMille|PerGram$|PerKilogram$/],
  ["Sound", /^decibels(A)?$/],
  ["Radiation", /becquerels|sieverts|[gG]ray$/],
  ["Currency", /^currency\d+$/],
];

export const UNIT_GROUP_ORDER = ["Common", ...GROUP_RULES.map(([group]) => group), "Other"];

const ABBREVIATIONS: Record<string, string> = {
  degreesFahrenheit: "°F",
  degreesCelsius: "°C",
  percent: "%",
  percentRelativeHumidity: "%RH",
  partsPerMillion: "ppm",
  partsPerBillion: "ppb",
  inchesOfWater: "in. w.c.",
  poundsForcePerSquareInch: "psi",
  cubicFeetPerMinute: "cfm",
  litersPerSecond: "L/s",
  usGallonsPerMinute: "gpm",
  kilowatts: "kW",
  kilowattHours: "kWh",
  amperes: "A",
  volts: "V",
  hertz: "Hz",
  revolutionsPerMinute: "rpm",
  pascals: "Pa",
  kilopascals: "kPa",
  btusPerHour: "BTU/h",
};

const WHOLE_LABELS: Record<string, string> = { noUnits: "No units", pH: "pH" };
const WORD_FIXES: Record<string, string> = { Btus: "BTUs", Us: "US" };

// "cubicFeetPerMinute" -> "Cubic Feet per Minute (cfm)".
export function unitLabel(name: string): string {
  const base =
    WHOLE_LABELS[name] ??
    name
      .replace(/([a-z])([A-Z])/g, "$1 $2")
      .replace(/([A-Za-z])(\d)/g, "$1 $2")
      .split(" ")
      .map((word, i) => {
        const lower = word.toLowerCase();
        if (i > 0 && (lower === "per" || lower === "of")) return lower;
        const cap = word[0].toUpperCase() + word.slice(1);
        return WORD_FIXES[cap] ?? cap;
      })
      .join(" ");
  const abbreviation = ABBREVIATIONS[name];
  return abbreviation ? `${base} (${abbreviation})` : base;
}

export function unitGroup(name: string): string {
  if (COMMON_UNITS.includes(name)) return "Common";
  return GROUP_RULES.find(([, rule]) => rule.test(name))?.[0] ?? "Other";
}

// The picker's options: None first, a stored value the list lacks next (so
// it still shows, and isn't silently replaced), then the groups.
// `units` undefined: the list hasn't loaded, so nothing is called non-standard yet.
export function unitOptions(units: string[] | undefined, current: string): ComboOption[] {
  const head: ComboOption[] = [{ value: "", label: "None", group: "" }];
  if (!units) return head;
  if (current && !units.includes(current)) {
    head.push({ value: current, label: `${current} (not a BACnet unit)`, group: "" });
  }
  const rank = (name: string) => {
    const group = unitGroup(name);
    return group === "Common" ? COMMON_UNITS.indexOf(name) : -1;
  };
  const listed = units
    .map((name) => ({ value: name, label: unitLabel(name), group: unitGroup(name), hint: name }))
    .sort(
      (a, b) =>
        UNIT_GROUP_ORDER.indexOf(a.group) - UNIT_GROUP_ORDER.indexOf(b.group) ||
        rank(a.value) - rank(b.value) ||
        a.label.localeCompare(b.label)
    );
  return [...head, ...listed];
}

// The unit as it reads after a number or in a label: "°F", "Cubic Meters".
export function unitShort(name: string | null | undefined): string {
  if (!name || name === "noUnits") return "";
  return ABBREVIATIONS[name] ?? unitLabel(name);
}

export function unitSuffix(name: string | null | undefined): string {
  const short = unitShort(name);
  return short ? ` (${short})` : "";
}
