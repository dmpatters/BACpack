import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import ValueControl from "./ValueControl";

const options = () => screen.getAllByRole("option").map((o) => o.textContent);

describe("ValueControl", () => {
  it("offers a binary value as Inactive and Active", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<ValueControl aria-label="Value" family="binary" value="inactive" onChange={onChange} />);
    expect(options()).toEqual(["Inactive", "Active"]);
    await user.selectOptions(screen.getByLabelText("Value"), "Active");
    expect(onChange).toHaveBeenCalledWith("active");
  });

  it("offers multi-state values by number and name, with an empty choice when asked", () => {
    render(<ValueControl aria-label="Value" family="multi-state" states={["Off", "Low"]} emptyLabel="None" value="2" onChange={() => {}} />);
    expect(options()).toEqual(["None", "1 · Off", "2 · Low"]);
    expect(screen.getByLabelText("Value")).toHaveValue("2");
  });

  it("keeps a stored value the options don't have", () => {
    render(<ValueControl aria-label="Value" family="multi-state" states={["Off", "On"]} value="7" onChange={() => {}} />);
    expect(screen.getByLabelText("Value")).toHaveValue("7");
    expect(screen.getByRole("option", { name: "7 · not a state" })).toBeInTheDocument();
  });

  it("asks for a choice when a required value is empty", () => {
    render(<ValueControl aria-label="Value" family="binary" required value="" onChange={() => {}} />);
    expect(screen.getByRole("option", { name: "Choose a value" })).toBeDisabled();
  });

  it("takes an analog value as a number, with the empty label as its placeholder", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<ValueControl aria-label="Value" family="analog" emptyLabel="Default (72 °F)" value="" onChange={onChange} />);
    const input = screen.getByLabelText("Value");
    expect(input).toHaveAttribute("type", "number");
    expect(input).toHaveAttribute("placeholder", "Default (72 °F)");
    await user.type(input, "5");
    expect(onChange).toHaveBeenLastCalledWith("5");
  });
});
