import { describe, expect, it } from "vitest";
import { parseId } from "./ids";

describe("parseId", () => {
  it.each([
    ["599002", 599002],
    ["0", 0],
  ])("reads %s", (param, id) => expect(parseId(param)).toBe(id));

  it.each([undefined, "", "abc", "new", "1e3", "-1", " 5", "5.0", "99999999999999999999"])(
    "refuses %s",
    (param) => expect(parseId(param)).toBeNull()
  );
});
