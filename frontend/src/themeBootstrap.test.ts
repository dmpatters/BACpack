import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { THEME_STORAGE_KEY } from "./utils/theme";

// index.html picks the theme before React loads, so a light-theme user never
// sees the dark default flash. It must agree with utils/theme.ts.
// Under jsdom import.meta.url isn't a file: URL, hence __dirname.
const html = readFileSync(resolve(__dirname, "../index.html"), "utf8");
const inline = /<script>([\s\S]*?)<\/script>/.exec(html)?.[1] ?? "";
const run = () => new Function(inline)();

function mockSystem(light: boolean) {
  vi.spyOn(window, "matchMedia").mockImplementation(
    (query: string) => ({ matches: light && query.includes("light"), media: query }) as unknown as MediaQueryList
  );
}

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
  delete document.documentElement.dataset.theme;
});

describe("index.html theme bootstrap", () => {
  it("is an inline script that reads the same storage key", () => {
    expect(inline).toContain(`"${THEME_STORAGE_KEY}"`);
  });

  it("applies a stored choice", () => {
    mockSystem(false);
    localStorage.setItem(THEME_STORAGE_KEY, "light");
    run();
    expect(document.documentElement.dataset.theme).toBe("light");
  });

  it("follows the system preference when nothing is stored", () => {
    mockSystem(true);
    run();
    expect(document.documentElement.dataset.theme).toBe("light");
  });

  it("follows the system preference when storage throws", () => {
    mockSystem(true);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    run();
    expect(document.documentElement.dataset.theme).toBe("light");
  });

  it("stays dark with no preference anywhere", () => {
    mockSystem(false);
    run();
    expect(document.documentElement.dataset.theme).toBe("dark");
  });
});
