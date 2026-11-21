import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import FlagChips from "./FlagChips";

const none = { bacnet_writable: false, trended: false, alarming_enabled: false, out_of_service_default: false, simulation: null };

describe("FlagChips", () => {
  it("shows every set flag in W T A O S order, each with a title", () => {
    render(<FlagChips obj={{ bacnet_writable: true, trended: true, alarming_enabled: true, out_of_service_default: true, simulation: { kind: "sine" } }} />);
    const chips = screen.getAllByTitle(/./);
    expect(chips.map((c) => c.querySelector("[aria-hidden]")?.textContent)).toEqual(["W", "T", "A", "O", "S"]);
    expect(chips.map((c) => c.getAttribute("title"))).toEqual([
      "BACnet writable",
      "Trended",
      "Alarming",
      "Out of service by default",
      "Simulated",
    ]);
    chips.forEach((c) => expect(c).toHaveClass("bp-flag"));
    expect(chips[0].parentElement).toHaveClass("bp-flags");
  });

  it("omits unset flags rather than greying them", () => {
    render(<FlagChips obj={{ ...none, trended: true }} />);
    expect(screen.getAllByTitle(/./).map((c) => c.querySelector("[aria-hidden]")?.textContent)).toEqual(["T"]);
  });

  it("shows an em dash with no flags", () => {
    render(<FlagChips obj={none} />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("says each set flag in words, not only in a hover title", () => {
    render(
      <FlagChips obj={{ bacnet_writable: true, trended: true, alarming_enabled: false, out_of_service_default: false, simulation: null }} />
    );
    const chip = screen.getByText("W").closest(".bp-flag")!;
    expect(chip).toHaveTextContent("WBACnet writable");
    expect(chip.querySelector(".bp-visually-hidden")).toHaveTextContent("BACnet writable");
    expect(screen.getByText("W")).toHaveAttribute("aria-hidden", "true");
    expect(screen.queryByText("A")).not.toBeInTheDocument();
  });
});
