// @vitest-environment node
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

// The SPA ships copies of the design system's CSS; docs/design-system is the
// source the design owner edits. Change both together.
const read = (url: URL) => readFileSync(url, "utf8").replace(/\r\n/g, "\n");

describe("design-system copies", () => {
  it.each([
    ["tokens.css", "../../../docs/design-system/tokens.css"],
    ["bundle.css", "../../../docs/design-system/components/bundle.css"],
  ])("%s matches docs/design-system", (local, source) => {
    expect(read(new URL(`./${local}`, import.meta.url))).toBe(read(new URL(source, import.meta.url)));
  });
});

describe("form layout", () => {
  // Fields placed straight in a fieldset (no FormGrid) need the same spacing
  // the form and the grid give theirs.
  it("spaces a fieldset's children", () => {
    const bundle = read(new URL("./bundle.css", import.meta.url));
    expect(bundle).toMatch(/\.bp-fieldset\s*\{[^}]*display:\s*grid;[^}]*gap:\s*var\(--space-4\)/);
  });

  // Side by side only where two form-wide columns fit; a phone keeps one column.
  it("stacks the columns below 1100px", () => {
    const bundle = read(new URL("./bundle.css", import.meta.url));
    expect(bundle).toMatch(/\.bp-columns\s*\{[^}]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*var\(--form-max\)\)\)/);
    expect(bundle).toMatch(/@media \(max-width: 1100px\)\s*\{\s*\.bp-columns\s*\{\s*grid-template-columns:\s*minmax\(0,\s*1fr\);/);
  });
});
