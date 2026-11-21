import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Layout from "./Layout";
import AuthGate from "./AuthGate";
import { authApi, versionApi } from "../api/endpoints";

vi.mock("../api/endpoints", () => ({
  versionApi: { get: vi.fn() },
  authApi: { state: vi.fn(), logout: vi.fn(), login: vi.fn() },
}));

function renderAt(path: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Layout>content</Layout>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe("Layout", () => {
  beforeEach(() => {
    vi.mocked(versionApi.get).mockResolvedValue({ version: "0.15.6", service: false });
    vi.mocked(authApi.state).mockResolvedValue({ mode: "none", signed_in: false, username: null });
  });

  it("shows the server's version in the nav", async () => {
    renderAt("/");
    expect(await screen.findByText("v0.15.6")).toBeInTheDocument();
  });

  it("marks API Docs as the current page on /docs", () => {
    renderAt("/docs");
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "API Docs" })).toHaveAttribute("aria-current", "page");
  });

  it("lists the sections in order, API Docs to /docs", () => {
    renderAt("/");
    const nav = screen.getByRole("navigation", { name: "Main" });
    const labels = within(nav)
      .getAllByRole("link")
      .map((a) => a.textContent);
    expect(labels).toEqual([
      "BACpack",
      "Devices",
      "Connectors",
      "Trend Storage",
      "Logs",
      "Network Settings",
      "Security",
      "Web Server",
      "Config",
      "API Docs",
      "About",
    ]);
    expect(within(nav).getByRole("link", { name: "API Docs" })).toHaveAttribute("href", "/docs");
    expect(within(nav).getByRole("link", { name: "Trend Storage" })).toHaveAttribute("href", "/trends");
  });

  it.each([
    ["/", "Devices"],
    ["/devices", "Devices"],
    ["/devices/599002/process", "Devices"],
    ["/trends", "Trend Storage"],
    ["/connectors/4", "Connectors"],
    ["/settings/network", "Network Settings"],
    ["/settings/security", "Security"],
    ["/settings/web", "Web Server"],
  ])("marks the current section at %s", (path, label) => {
    renderAt(path);
    const nav = screen.getByRole("navigation", { name: "Main" });
    const current = within(nav)
      .getAllByRole("link")
      .filter((a) => a.getAttribute("aria-current") === "page");
    expect(current.map((a) => a.textContent)).toEqual([label]);
  });

  it("offers the theme toggle and wraps content in the page frame", () => {
    renderAt("/");
    expect(screen.getByRole("button", { name: /theme$/ })).toBeInTheDocument();
    expect(screen.getByRole("main")).toHaveClass("bp-page");
  });

  describe("Sign out", () => {
    function renderGated() {
      const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      return render(
        <QueryClientProvider client={queryClient}>
          <MemoryRouter>
            <AuthGate>
              <Layout>content</Layout>
            </AuthGate>
          </MemoryRouter>
        </QueryClientProvider>
      );
    }

    it("shows only in login mode and posts the logout", async () => {
      vi.mocked(authApi.state).mockResolvedValue({ mode: "login", signed_in: true, username: "admin" });
      vi.mocked(authApi.logout).mockResolvedValue(undefined);
      renderGated();
      await userEvent.click(await screen.findByRole("button", { name: "Sign out" }));
      await waitFor(() => expect(authApi.logout).toHaveBeenCalled());
      expect(await screen.findByRole("heading", { name: "Sign in to BACpack" })).toBeInTheDocument();
    });

    it("is not offered when the install runs without a login", async () => {
      renderGated();
      await screen.findByText("content");
      expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
    });
  });
});
