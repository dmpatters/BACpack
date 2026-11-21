import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import IsoEcho, { isoText } from "./IsoEcho";

describe("isoText", () => {
  it("formats native input values in ISO order and 24-hour time", () => {
    expect(isoText("2026-10-16", "date")).toBe("2026-10-16");
    expect(isoText("14:30", "time")).toBe("14:30");
    expect(isoText("14:30:05", "time")).toBe("14:30:05");
    expect(isoText("2026-10-16T14:30", "datetime")).toBe("2026-10-16 14:30");
    expect(isoText("2026-10-16T14:30:05", "datetime")).toBe("2026-10-16 14:30:05");
  });
  it("gives nothing for an empty or half-typed value", () => {
    expect(isoText("", "date")).toBeNull();
    expect(isoText("2026-1", "date")).toBeNull();
    expect(isoText("25:00", "time")).toBeNull();
  });
});

describe("IsoEcho", () => {
  it("shows the value with its zone", () => {
    render(<IsoEcho value="2026-10-16T14:30" kind="datetime" zone="UTC" />);
    expect(screen.getByTestId("iso-echo")).toHaveTextContent("2026-10-16 14:30 UTC");
  });
  it("renders nothing without a value", () => {
    const { container } = render(<IsoEcho value="" kind="time" />);
    expect(container).toBeEmptyDOMElement();
  });
});
