import { afterEach, describe, expect, it, vi } from "vitest";
import { THEME_STORAGE_KEY, applyTheme, initialTheme, saveTheme, storedTheme } from "./theme";

function mockSystem(light: boolean) {
  vi.spyOn(window, "matchMedia").mockImplementation(
    (query: string) =>
      ({
        matches: light && query.includes("light"),
        media: query,
        addEventListener: () => {},
        removeEventListener: () => {},
      }) as unknown as MediaQueryList
  );
}

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
  delete document.documentElement.dataset.theme;
});

describe("theme", () => {
  it("defaults to dark with nothing stored and no light preference", () => {
    mockSystem(false);
    expect(initialTheme()).toBe("dark");
  });

  it("follows a light system preference until the user chooses", () => {
    mockSystem(true);
    expect(initialTheme()).toBe("light");
    saveTheme("dark");
    expect(initialTheme()).toBe("dark");
  });

  it("ignores a stored value that isn't a theme", () => {
    localStorage.setItem(THEME_STORAGE_KEY, "purple");
    expect(storedTheme()).toBeNull();
  });

  it("works when storage throws", () => {
    mockSystem(true);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(initialTheme()).toBe("light");
    expect(() => saveTheme("dark")).not.toThrow();
  });

  it("sets data-theme on <html>", () => {
    applyTheme("light");
    expect(document.documentElement.dataset.theme).toBe("light");
  });
});
