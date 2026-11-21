import type { ReactNode } from "react";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, RouterProvider, createMemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import App, { Root } from "./App";

// Layout fetches the version for the nav bar; only the routing is under test.
vi.mock("./components/Layout", () => ({ default: ({ children }: { children: ReactNode }) => <>{children}</> }));
vi.mock("./pages/Dashboard", () => ({ default: () => <p>device home</p> }));
vi.mock("./api/endpoints", () => ({
  authApi: { state: vi.fn().mockResolvedValue({ mode: "none", signed_in: false, username: null }) },
}));

describe("App routes", () => {
  it.each(["/", "/devices"])("shows the device list at %s", (path) => {
    render(
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    );
    expect(screen.getByText("device home")).toBeInTheDocument();
  });

  // A path no route matches, such as the nav label instead of its route.
  it("shows a not-found page for an unknown path", () => {
    render(
      <MemoryRouter initialEntries={["/trend-storage"]}>
        <App />
      </MemoryRouter>
    );
    expect(screen.getByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    expect(screen.getByText(/Nothing is at \/trend-storage/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to devices" })).toHaveAttribute("href", "/");
  });

  it("serves the routes from the root element under a data router", async () => {
    const router = createMemoryRouter([{ path: "*", element: <Root /> }], { initialEntries: ["/devices"] });
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    );
    expect(await screen.findByText("device home")).toBeInTheDocument();
  });
});
