import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Security from "./Security";
import { apiKeysApi, loginSettingsApi } from "../api/endpoints";
import { UnsavedChangesProvider } from "../components/UnsavedChanges";
import { inSettingsColumn } from "../testing/columns";

vi.mock("../api/endpoints", () => ({
  loginSettingsApi: { get: vi.fn(), update: vi.fn(), enable: vi.fn(), disable: vi.fn() },
  apiKeysApi: { list: vi.fn(), create: vi.fn(), revoke: vi.fn() },
  authApi: { state: vi.fn().mockResolvedValue({ mode: "login", signed_in: true, username: "admin" }) },
}));

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <UnsavedChangesProvider>
          <Security />
        </UnsavedChangesProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
  return userEvent.setup();
}

describe("Security page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(loginSettingsApi.get).mockResolvedValue({ mode: "login", username: "admin" });
    vi.mocked(apiKeysApi.list).mockResolvedValue([]);
  });

  it("stacks every panel in the one settings column, with the header outside it", async () => {
    renderPage();
    for (const name of ["Username", "Password", "Run without login", "New key"]) {
      expect(inSettingsColumn(await screen.findByRole("group", { name }))).toBe(true);
    }
    expect(inSettingsColumn(screen.getByRole("heading", { name: "Security" }))).toBe(false);
  });

  it("keeps the API keys table at full width, outside the settings column", async () => {
    vi.mocked(apiKeysApi.list).mockResolvedValue([
      { id: 1, name: "grafana", prefix: "bpk_abcdefgh", created: "2026-11-07T10:00:00+00:00", last_used: null },
    ]);
    renderPage();
    const table = await screen.findByRole("table", { name: "API keys" });
    expect(inSettingsColumn(table)).toBe(false);
    expect(inSettingsColumn(screen.getByRole("group", { name: "New key" }))).toBe(true);
  });

  it("asks for the current password to change the password", async () => {
    vi.mocked(loginSettingsApi.update).mockResolvedValue({ mode: "login", username: "admin" });
    const user = renderPage();
    const password = await screen.findByRole("group", { name: "Password" });
    const change = within(password).getByRole("button", { name: "Change password" });
    expect(change).toBeDisabled();
    await user.type(within(password).getByLabelText("Current password"), "old password here");
    await user.type(within(password).getByLabelText("New password"), "a different long password");
    await user.type(within(password).getByLabelText("New password again"), "a different long password");
    await user.click(change);
    expect(loginSettingsApi.update).toHaveBeenCalledWith({
      new_password: "a different long password",
      current_password: "old password here",
    });
  });

  it("refuses a short or mismatched new password before calling the server", async () => {
    const user = renderPage();
    const password = await screen.findByRole("group", { name: "Password" });
    await user.type(within(password).getByLabelText("Current password"), "old password here");
    await user.type(within(password).getByLabelText("New password"), "short");
    await user.click(within(password).getByRole("button", { name: "Change password" }));
    expect(await within(password).findByText(/at least 12 characters/, { selector: "[role=alert]" })).toBeInTheDocument();
    expect(within(password).getByLabelText("New password")).toHaveAccessibleDescription(
      "The new password needs at least 12 characters."
    );
    expect(loginSettingsApi.update).not.toHaveBeenCalled();
  });

  it("renames the user without the password", async () => {
    vi.mocked(loginSettingsApi.update).mockResolvedValue({ mode: "login", username: "owner" });
    const user = renderPage();
    const group = await screen.findByRole("group", { name: "Username" });
    await user.clear(within(group).getByLabelText("Username"));
    await user.type(within(group).getByLabelText("Username"), "owner");
    await user.click(within(group).getByRole("button", { name: "Save username" }));
    expect(loginSettingsApi.update).toHaveBeenCalledWith({ username: "owner" });
  });

  it("needs the password to switch to none", async () => {
    vi.mocked(loginSettingsApi.disable).mockResolvedValue({ mode: "none", username: null });
    const user = renderPage();
    const group = await screen.findByRole("group", { name: "Run without login" });
    const button = within(group).getByRole("button", { name: "Run without login" });
    expect(button).toBeDisabled();
    await user.type(within(group).getByLabelText("Current password"), "old password here");
    await user.click(button);
    expect(loginSettingsApi.disable).toHaveBeenCalledWith({ current_password: "old password here" });
  });

  it("offers only Create login in none mode", async () => {
    vi.mocked(loginSettingsApi.get).mockResolvedValue({ mode: "none", username: null });
    renderPage();
    expect(await screen.findByRole("group", { name: "Create a login" })).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Password" })).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Run without login" })).not.toBeInTheDocument();
  });

  it("creates a login from none mode", async () => {
    vi.mocked(loginSettingsApi.get).mockResolvedValue({ mode: "none", username: null });
    vi.mocked(loginSettingsApi.enable).mockResolvedValue({ mode: "login", username: "owner" });
    const user = renderPage();
    const group = await screen.findByRole("group", { name: "Create a login" });
    await user.type(within(group).getByLabelText("Username"), "owner");
    await user.type(within(group).getByLabelText("Password"), "a long enough password");
    await user.type(within(group).getByLabelText("Password again"), "a long enough password");
    await user.click(within(group).getByRole("button", { name: "Create login" }));
    expect(loginSettingsApi.enable).toHaveBeenCalledWith({ username: "owner", password: "a long enough password" });
  });

  it("shows a new key once with a copy button, and not after dismissal", async () => {
    vi.mocked(apiKeysApi.create).mockResolvedValue({
      id: 1,
      name: "grafana",
      prefix: "bpk_abcdefgh",
      created: "2026-11-07T10:00:00+00:00",
      last_used: null,
      key: "bpk_abcdefghSECRETSECRET",
    });
    const user = renderPage();
    await user.type(await screen.findByLabelText("Name"), "grafana");
    await user.click(screen.getByRole("button", { name: "Create key" }));
    expect(await screen.findByTestId("new-api-key")).toHaveTextContent("bpk_abcdefghSECRETSECRET");

    await user.click(screen.getByRole("button", { name: "Copy" }));
    expect(await navigator.clipboard.readText()).toBe("bpk_abcdefghSECRETSECRET");
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Done" }));
    expect(screen.queryByTestId("new-api-key")).not.toBeInTheDocument();
  });

  it("lists the keys without ever showing a full key", async () => {
    vi.mocked(apiKeysApi.list).mockResolvedValue([
      { id: 1, name: "grafana", prefix: "bpk_abcdefgh", created: "2026-11-07T10:00:00+00:00", last_used: null },
    ]);
    renderPage();
    const table = await screen.findByRole("table", { name: "API keys" });
    expect(within(table).getByText("bpk_abcdefgh")).toBeInTheDocument();
    expect(within(table).getByText("2026-11-07 10:00:00 UTC")).toBeInTheDocument();
    expect(within(table).getByText("never")).toBeInTheDocument();
  });

  it("revokes a key after confirmation and drops the row", async () => {
    vi.mocked(apiKeysApi.list).mockResolvedValueOnce([
      { id: 7, name: "grafana", prefix: "bpk_abcdefgh", created: "2026-11-07T10:00:00+00:00", last_used: null },
    ]);
    vi.mocked(apiKeysApi.revoke).mockResolvedValue(undefined);
    const user = renderPage();
    await user.click(await screen.findByRole("button", { name: "Revoke grafana" }));
    vi.mocked(apiKeysApi.list).mockResolvedValue([]);
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Revoke" }));
    expect(apiKeysApi.revoke).toHaveBeenCalledWith(7);
    expect(await screen.findByText("No API keys yet.")).toBeInTheDocument();
  });

  it("keeps the no-keys line in the centred column", async () => {
    vi.mocked(apiKeysApi.list).mockResolvedValue([]);
    renderPage();
    expect(inSettingsColumn(await screen.findByText("No API keys yet."))).toBe(true);
  });
});
