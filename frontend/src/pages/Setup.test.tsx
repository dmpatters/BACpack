import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Setup from "./Setup";
import { authApi } from "../api/endpoints";

import { inSettingsColumn } from "../testing/columns";

vi.mock("../api/endpoints", () => ({
  authApi: { state: vi.fn(), setup: vi.fn() },
}));

function renderSetup() {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <Setup />
    </QueryClientProvider>
  );
  return userEvent.setup();
}

describe("Setup", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(authApi.setup).mockResolvedValue({ mode: "login" });
  });

  it("holds the title, the choice and the form in one centred column", () => {
    renderSetup();
    expect(inSettingsColumn(screen.getByRole("heading", { name: "Set up BACpack" }))).toBe(true);
    expect(inSettingsColumn(screen.getByLabelText("Username"))).toBe(true);
  });

  it("needs matching passwords of 12 or more characters to create a login", async () => {
    const user = renderSetup();
    await user.type(screen.getByLabelText("Username"), "owner");
    await user.type(screen.getByLabelText("Password"), "short");
    await user.type(screen.getByLabelText("Password again"), "short");
    await user.click(screen.getByRole("button", { name: "Create login" }));
    expect(await screen.findByText(/at least 12 characters/, { selector: "[role=alert]" })).toBeInTheDocument();
    // Each rule's message sits under the field it is about.
    expect(screen.getByLabelText("Password")).toHaveAccessibleDescription("The password needs at least 12 characters.");
    expect(screen.getByLabelText("Password again")).not.toHaveAttribute("aria-invalid");
    expect(authApi.setup).not.toHaveBeenCalled();

    await user.clear(screen.getByLabelText("Password"));
    await user.type(screen.getByLabelText("Password"), "a long enough password");
    await user.click(screen.getByRole("button", { name: "Create login" }));
    expect(await screen.findByText("The two passwords differ.")).toBeInTheDocument();
    expect(screen.getByLabelText("Password again")).toHaveAccessibleDescription("The two passwords differ.");
    expect(screen.getByLabelText("Password")).not.toHaveAttribute("aria-invalid");
    expect(authApi.setup).not.toHaveBeenCalled();
  });

  it("posts the login when the form is right", async () => {
    const user = renderSetup();
    await user.type(screen.getByLabelText("Username"), " owner ");
    await user.type(screen.getByLabelText("Password"), "a long enough password");
    await user.type(screen.getByLabelText("Password again"), "a long enough password");
    await user.click(screen.getByRole("button", { name: "Create login" }));
    expect(authApi.setup).toHaveBeenCalledWith({ mode: "login", username: "owner", password: "a long enough password" });
  });

  it("keeps Run without login disabled until the box is ticked", async () => {
    const user = renderSetup();
    await user.click(screen.getByRole("radio", { name: "Run without login" }));
    const button = screen.getByRole("button", { name: "Run without login" });
    expect(button).toBeDisabled();
    await user.click(screen.getByRole("checkbox"));
    expect(button).toBeEnabled();
  });

  it("posts the none choice with its confirmation", async () => {
    const user = renderSetup();
    await user.click(screen.getByRole("radio", { name: "Run without login" }));
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "Run without login" }));
    expect(authApi.setup).toHaveBeenCalledWith({ mode: "none", confirm: true });
  });

  it("shows the server's refusal", async () => {
    vi.mocked(authApi.setup).mockRejectedValue(new Error("a login mode has already been chosen"));
    const user = renderSetup();
    await user.type(screen.getByLabelText("Username"), "owner");
    await user.type(screen.getByLabelText("Password"), "a long enough password");
    await user.type(screen.getByLabelText("Password again"), "a long enough password");
    await user.click(screen.getByRole("button", { name: "Create login" }));
    expect(await screen.findByText("a login mode has already been chosen")).toBeInTheDocument();
  });
});
