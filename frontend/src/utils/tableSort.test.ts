import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { matchesSearch, sortRows, useSort } from "./tableSort";

const rows = [
  { name: "AV-10", n: 3 },
  { name: "av-2", n: 1 },
  { name: "AV-1", n: 2 },
];
const keys = { name: (r: (typeof rows)[number]) => r.name, n: (r: (typeof rows)[number]) => r.n };

describe("sortRows", () => {
  it("leaves the order alone with no sort, and doesn't mutate", () => {
    const copy = [...rows];
    expect(sortRows(rows, null, keys)).toEqual(rows);
    expect(rows).toEqual(copy);
  });

  it("sorts names naturally and case-insensitively", () => {
    expect(sortRows(rows, { key: "name", dir: "ascending" }, keys).map((r) => r.name)).toEqual(["AV-1", "av-2", "AV-10"]);
  });

  it("sorts numbers descending", () => {
    expect(sortRows(rows, { key: "n", dir: "descending" }, keys).map((r) => r.n)).toEqual([3, 2, 1]);
  });
});

describe("useSort", () => {
  it("sorts ascending on first use of a column and flips after", () => {
    const { result } = renderHook(() => useSort<"name" | "n">());
    expect(result.current.sort).toBeNull();
    act(() => result.current.toggle("name"));
    expect(result.current.sort).toEqual({ key: "name", dir: "ascending" });
    act(() => result.current.toggle("name"));
    expect(result.current.sort).toEqual({ key: "name", dir: "descending" });
    act(() => result.current.toggle("n"));
    expect(result.current.sort).toEqual({ key: "n", dir: "ascending" });
  });
});

describe("matchesSearch", () => {
  it("matches any field, ignoring case and surrounding space", () => {
    expect(matchesSearch("  zone ", "AV-1", "Zone temp")).toBe(true);
    expect(matchesSearch("analog-value:3", "x", "analog-value:3")).toBe(true);
    expect(matchesSearch("pump", "AV-1", null, undefined, 7)).toBe(false);
    expect(matchesSearch("", "anything")).toBe(true);
  });
});
