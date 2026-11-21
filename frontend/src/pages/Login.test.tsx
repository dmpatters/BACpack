import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Login from "./Login";
import { authApi } from "../api/endpoints";
import { ApiError } from "../api/http";

vi.mock("../api/endpoints", () => ({
  authApi: { state: vi.fn(), login: vi.fn() },
}));

function renderLogin() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  vi.spyOn(queryClient, "invalidateQueries");
  render(
    <QueryClientProvider client={queryClient}>
      <Login />
    </QueryClientProvider>
  );
  return queryClient;
}

async function fillIn() {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Username"), "admin");
  await user.type(screen.getByLabelText("Password"), "a long enough password");
  await user.click(screen.getByRole("button", { name: "Sign in" }));
}

describe("Login", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows the sign-in form in the narrow centred panel", () => {
    renderLogin();
    expect(screen.getByLabelText("Username").closest(".bp-auth")).not.toBeNull();
    expect(screen.getByRole("heading", { name: "Sign in to BACpack" }).closest(".bp-auth")).not.toBeNull();
  });

  it("signs in and refetches the auth state", async () => {
    vi.mocked(authApi.login).mockResolvedValue({ mode: "login", signed_in: true, username: "admin" });
    const queryClient = renderLogin();
    await fillIn();
    expect(authApi.login).toHaveBeenCalledWith({ username: "admin", password: "a long enough password" });
    await waitFor(() => expect(queryClient.invalidateQueries).toHaveBeenCalledWith({ queryKey: ["authState"] }));
  });

  it("shows the wrong-credentials message on 401", async () => {
    vi.mocked(authApi.login).mockRejectedValue(new ApiError("wrong username or password", 401));
    renderLogin();
    await fillIn();
    expect(await screen.findByText("Wrong username or password.")).toBeInTheDocument();
  });

  it("shows the wait time on 429", async () => {
    vi.mocked(authApi.login).mockRejectedValue(new ApiError("too many failed logins", 429, undefined, 900));
    renderLogin();
    await fillIn();
    expect(await screen.findByText(/Try again in about 15 minutes/)).toBeInTheDocument();
  });

  it("keeps Sign in disabled until both fields are filled", () => {
    renderLogin();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeDisabled();
  });
});
