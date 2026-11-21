// @vitest-environment node
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { compile } from "@tailwindcss/node";

const srcDir = fileURLToPath(new URL(".", import.meta.url));

async function build(candidates: string[]): Promise<string> {
  const css = readFileSync(new URL("./styles.css", import.meta.url), "utf8");
  const compiler = await compile(css, { base: srcDir, onDependency: () => {} });
  return compiler.build(candidates);
}

describe("styles.css Tailwind theme", () => {
  it("resolves spacing utilities to the design tokens", async () => {
    const out = await build(["gap-4"]);
    expect(out).toContain("var(--spacing-4)");
    expect(out).toMatch(/--spacing-4:\s*var\(--space-4\)/);
  });

  it("generates no palette colour, font size, radius or shadow utilities", async () => {
    const out = await build(["text-red-500", "bg-blue-600", "text-xl", "rounded-lg", "shadow-md", "p-9"]);
    expect(out).not.toMatch(/\.(text-red-500|bg-blue-600|text-xl|rounded-lg|shadow-md|p-9)\b/);
  });

  it("keeps layout utilities and the 640px breakpoint", async () => {
    const out = await build(["flex", "hidden", "sm:grid-cols-2"]);
    expect(out).toContain(".flex");
    expect(out).toContain("640px");
  });

  it("includes the design system's tokens and component classes", async () => {
    const out = await build([]);
    expect(out).toContain("--surface-raised");
    expect(out).toContain(".bp-btn");
  });

  it("leaves tables, forms and muted text to the design system", async () => {
    const out = await build([]);
    expect(out).not.toMatch(/table:not\(\.bp-table\)/);
    expect(out).not.toMatch(/input:not\(\.bp-input\)/);
    expect(out).not.toMatch(/label:not\(\.bp-label/);
    expect(out).not.toMatch(/(^|[\s}])fieldset\s*\{/);
    expect(out).not.toMatch(/(^|[\s}])\.(muted|actions)\s*\{/);
    expect(out).not.toMatch(/td \.url/);
  });

  it("spaces a form or fieldset that stands on the page, not one inside another", async () => {
    const out = await build([]);
    expect(out).toMatch(/\.bp-form:not\(\.bp-fieldset \.bp-form\)[^{]*\{[^}]*margin-block/);
    expect(out).toMatch(/\.bp-fieldset:not\(\.bp-form \.bp-fieldset, \.bp-fieldset \.bp-fieldset\)[^{]*\{[^}]*margin-block/);
    // A fieldset holding its form, or standing alone, stops where a form does.
    expect(out).toMatch(/\.bp-fieldset:not\(\.bp-form \.bp-fieldset, \.bp-fieldset \.bp-fieldset\)\s*\{[^}]*max-width:\s*var\(--form-max\)/);
  });
});
