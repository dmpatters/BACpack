import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import Button, { buttonClass } from "./Button";

describe("Button", () => {
  it("is secondary by default", () => {
    expect(buttonClass()).toBe("bp-btn");
  });

  it("adds the variant and small modifiers", () => {
    expect(buttonClass("primary")).toBe("bp-btn bp-btn--primary");
    expect(buttonClass("delete", true)).toBe("bp-btn bp-btn--delete bp-btn--sm");
  });

  it("renders a button with the classes and passes props through", () => {
    render(
      <Button variant="danger" type="submit" disabled className="extra">
        Delete device
      </Button>
    );
    const button = screen.getByRole("button", { name: "Delete device" });
    expect(button).toHaveClass("bp-btn", "bp-btn--danger", "extra");
    expect(button).toHaveAttribute("type", "submit");
    expect(button).toBeDisabled();
  });
});
