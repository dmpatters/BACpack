import { describe, expect, it } from "vitest";
import { COMMON_UNITS, UNIT_GROUP_ORDER, unitGroup, unitLabel, unitOptions, unitShort, unitSuffix } from "./units";

const ALL_UNITS =
  "ampereSeconds ampereSquareHours ampereSquareMeters amperes amperesPerMeter amperesPerSquareMeter bars becquerels btus btusPerHour btusPerPound btusPerPoundDryAir candelas candelasPerSquareMeter centimeters centimetersOfMercury centimetersOfWater cubicFeet cubicFeetPerDay cubicFeetPerHour cubicFeetPerMinute cubicFeetPerSecond cubicMeters cubicMetersPerDay cubicMetersPerHour cubicMetersPerMinute cubicMetersPerSecond currency1 currency10 currency2 currency3 currency4 currency5 currency6 currency7 currency8 currency9 cyclesPerHour cyclesPerMinute days decibels decibelsA decibelsMillivolt decibelsVolt degreeDaysCelsius degreeDaysFahrenheit degreesAngular degreesCelsius degreesCelsiusPerHour degreesCelsiusPerMinute degreesFahrenheit degreesFahrenheitPerHour degreesFahrenheitPerMinute degreesKelvin degreesKelvinPerHour degreesKelvinPerMinute degreesPhase deltaDegreesFahrenheit deltaDegreesKelvin farads feet feetPerMinute feetPerSecond footCandles grams gramsOfWaterPerKilogramDryAir gramsPerCubicCentimeter gramsPerCubicMeter gramsPerGram gramsPerKilogram gramsPerLiter gramsPerMilliliter gramsPerMinute gramsPerSecond gramsPerSquareMeter gray hectopascals henrys hertz horsepower hours hundredthsSeconds imperialGallons imperialGallonsPerMinute inches inchesOfMercury inchesOfWater jouleSeconds joules joulesPerCubicMeter joulesPerDegreeKelvin joulesPerHours joulesPerKilogramDegreeKelvin joulesPerKilogramDryAir kiloBtus kiloBtusPerHour kilobecquerels kilograms kilogramsPerCubicMeter kilogramsPerHour kilogramsPerKilogram kilogramsPerMinute kilogramsPerSecond kilohertz kilohms kilojoules kilojoulesPerDegreeKelvin kilojoulesPerKilogram kilojoulesPerKilogramDryAir kilometers kilometersPerHour kilopascals kilovoltAmpereHours kilovoltAmpereHoursReactive kilovoltAmperes kilovoltAmperesReactive kilovolts kilowattHours kilowattHoursPerSquareFoot kilowattHoursPerSquareMeter kilowattHoursReactive kilowatts liters litersPerHour litersPerMinute litersPerSecond lumens luxes megaBtus megabecquerels megahertz megajoules megajoulesPerDegreeKelvin megajoulesPerKilogramDryAir megajoulesPerSquareFoot megajoulesPerSquareMeter megavoltAmpereHours megavoltAmpereHoursReactive megavoltAmperes megavoltAmperesReactive megavolts megawattHours megawattHoursReactive megawatts megohms meters metersPerHour metersPerMinute metersPerSecond metersPerSecondPerSecond microSiemens microgramsPerCubicMeter microgramsPerLiter microgray micrometers microsieverts microsievertsPerHour milesPerHour milliamperes millibars milligrams milligramsPerCubicMeter milligramsPerGram milligramsPerKilogram milligramsPerLiter milligray milliliters millilitersPerSecond millimeters millimetersOfMercury millimetersOfWater millimetersPerMinute millimetersPerSecond milliohms milliseconds millisiemens millisieverts millivolts milliwatts minutes minutesPerDegreeKelvin months nanogramsPerCubicMeter nephelometricTurbidityUnit newton newtonMeters newtonSeconds newtonsPerMeter noUnits ohmMeterPerSquareMeter ohmMeters ohms pH partsPerBillion partsPerMillion pascalSeconds pascals perHour perMille perMinute perSecond percent percentObscurationPerFoot percentObscurationPerMeter percentPerSecond percentRelativeHumidity poundsForcePerSquareInch poundsMass poundsMassPerHour poundsMassPerMinute poundsMassPerSecond powerFactor psiPerDegreeFahrenheit radians radiansPerSecond revolutionsPerMinute seconds siemens siemensPerMeter sieverts squareCentimeters squareFeet squareInches squareMeters squareMetersPerNewton teslas therms tonHours tons tonsPerHour tonsRefrigeration usGallons usGallonsPerHour usGallonsPerMinute voltAmpereHours voltAmpereHoursReactive voltAmperes voltAmperesReactive volts voltsPerDegreeKelvin voltsPerMeter voltsSquareHours wattHours wattHoursPerCubicMeter wattHoursReactive watts wattsPerMeterPerDegreeKelvin wattsPerSquareFoot wattsPerSquareMeter wattsPerSquareMeterDegreeKelvin webers weeks years".split(" ");

describe("unitLabel", () => {
  it.each([
    ["degreesFahrenheit", "Degrees Fahrenheit (°F)"],
    ["cubicFeetPerMinute", "Cubic Feet per Minute (cfm)"],
    ["usGallonsPerMinute", "US Gallons per Minute (gpm)"],
    ["kiloBtusPerHour", "Kilo BTUs per Hour"],
    ["inchesOfMercury", "Inches of Mercury"],
    ["currency10", "Currency 10"],
    ["noUnits", "No units"],
    ["pH", "pH"],
  ])("%s reads %s", (name, label) => {
    expect(unitLabel(name)).toBe(label);
  });
});

describe("unitGroup", () => {
  it.each([
    ["degreesFahrenheit", "Common"],
    ["degreesKelvin", "Temperature"],
    ["inchesOfMercury", "Pressure"],
    ["cubicMetersPerHour", "Flow"],
    ["kilovoltAmperes", "Electrical"],
    ["btusPerHour", "Power"],
    ["megawattHours", "Energy"],
    ["pH", "Concentration and water quality"],
    ["candelasPerSquareMeter", "Light"],
    ["newtonMeters", "Force and torque"],
    ["millimetersPerSecond", "Length, area and speed"],
    ["jouleSeconds", "Other"],
  ])("%s is in %s", (name, group) => {
    expect(unitGroup(name)).toBe(group);
  });

  it("files every unit somewhere, leaving only three in Other", () => {
    for (const name of ALL_UNITS) expect(UNIT_GROUP_ORDER).toContain(unitGroup(name));
    expect(ALL_UNITS.filter((n) => unitGroup(n) === "Other")).toEqual(["jouleSeconds", "minutesPerDegreeKelvin", "pascalSeconds"]);
  });
});

describe("unitOptions", () => {
  it("starts with None, then the common units in their own order, each unit once", () => {
    const options = unitOptions(ALL_UNITS, "");
    expect(options[0]).toEqual({ value: "", label: "None", group: "" });
    expect(options.slice(1, 1 + COMMON_UNITS.length).map((o) => o.value)).toEqual(COMMON_UNITS);
    expect(options.slice(1).map((o) => o.value).sort()).toEqual([...ALL_UNITS].sort());
    expect(options.find((o) => o.value === "degreesKelvin")).toMatchObject({ group: "Temperature", hint: "degreesKelvin" });
  });

  it("orders the groups by UNIT_GROUP_ORDER and labels alphabetically inside one", () => {
    const groups = [...new Set(unitOptions(ALL_UNITS, "").slice(1).map((o) => o.group))];
    expect(groups).toEqual(UNIT_GROUP_ORDER.filter((g) => groups.includes(g)));
    const temps = unitOptions(ALL_UNITS, "").filter((o) => o.group === "Temperature").map((o) => o.label);
    expect(temps).toEqual([...temps].sort((a, b) => a.localeCompare(b)));
  });

  it("keeps a stored value the list doesn't have, marked", () => {
    const options = unitOptions(["degreesFahrenheit"], "degF");
    expect(options[1]).toEqual({ value: "degF", label: "degF (not a BACnet unit)", group: "" });
  });
});

describe("unitShort", () => {
  it("prefers the abbreviation, falls back to the label, and drops no units", () => {
    expect(unitShort("degreesFahrenheit")).toBe("°F");
    expect(unitShort("cubicMeters")).toBe("Cubic Meters");
    expect(unitShort("noUnits")).toBe("");
    expect(unitShort(null)).toBe("");
    expect(unitSuffix("percent")).toBe(" (%)");
    expect(unitSuffix("")).toBe("");
  });
});
