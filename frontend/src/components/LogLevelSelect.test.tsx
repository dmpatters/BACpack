import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import LogLevelSelect from "./LogLevelSelect";

describe("LogLevelSelect", () => {
  it("labels the control as a minimum rather than an exact match", () => {
    render(<LogLevelSelect value="INFO" onChange={() => {}} />);
    expect(screen.getByLabelText(/minimum level/i)).toBeInTheDocument();
  });

  it("spells out that a level includes everything above it", () => {
    render(<LogLevelSelect value="INFO" onChange={() => {}} />);
    expect(screen.getByRole("option", { name: "INFO and above" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "WARNING and above" })).toBeInTheDocument();
  });

  it("does not claim anything is above CRITICAL", () => {
    render(<LogLevelSelect value="INFO" onChange={() => {}} />);
    expect(screen.getByRole("option", { name: "CRITICAL only" })).toBeInTheDocument();
  });

  it("reports the chosen level to its parent", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<LogLevelSelect value="INFO" onChange={onChange} />);
    await user.selectOptions(screen.getByLabelText(/minimum level/i), "WARNING");
    expect(onChange).toHaveBeenCalledWith("WARNING");
  });
});
