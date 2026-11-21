import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import ConnectorHealthPill, { connectorHealth } from "./ConnectorHealth";
import type { ConnectorStatusDTO } from "../api/types";

const base: ConnectorStatusDTO = { last_poll_at: null, last_success: null, last_error: null, consecutive_failures: 0 };

describe("connectorHealth", () => {
  it("covers each state", () => {
    expect(connectorHealth(undefined)).toBe("not running");
    expect(connectorHealth({ ...base, suppressed_by_dcc: true, last_success: false })).toBe("suppressed");
    expect(connectorHealth({ ...base, last_success: false })).toBe("failing");
    expect(connectorHealth({ ...base, last_success: true })).toBe("ok");
    expect(connectorHealth(base)).toBe("waiting");
  });

  it("uses write results for a connector with only write mappings", () => {
    expect(connectorHealth({ ...base, write_status: { "3": { error: null, last_sent_at: "t" } } })).toBe("ok");
    expect(connectorHealth({ ...base, write_status: { "3": { error: "reject", last_sent_at: null } } })).toBe("failing");
  });
});

describe("ConnectorHealthPill", () => {
  it.each([
    [{ ...base, last_success: false, consecutive_failures: 3 }, true, "failing (3)", "bp-pill--danger"],
    [{ ...base, last_success: true }, true, "ok", "bp-pill--ok"],
    [{ ...base, suppressed_by_dcc: true }, true, "suppressed by DCC", "bp-pill--warn"],
    [undefined, true, "not running", null],
    [base, true, "waiting…", null],
    [{ ...base, last_success: true }, false, "disabled", null],
  ] as const)("%#: %s", (status, enabled, text, tone) => {
    render(<ConnectorHealthPill status={status} enabled={enabled} />);
    const pill = screen.getByText(text);
    expect(pill).toHaveClass("bp-pill");
    if (tone) expect(pill).toHaveClass(tone);
  });
});
