import { useState } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import AuthGate, { useSignOut } from "./AuthGate";
import { api, setAuthHandlers } from "../api/http";
import { authApi } from "../api/endpoints";

vi.mock("../api/endpoints", () => ({
  authApi: { state: vi.fn(), login: vi.fn(), setup: vi.fn(), logout: vi.fn() },
}));

function renderGate(child: React.ReactNode = <p>the app</p>) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthGate>{child}</AuthGate>
    </QueryClientProvider>
  );
}

describe("AuthGate", () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(() => {
    setAuthHandlers({});
    vi.unstubAllGlobals();
  });

  it("shows Setup when no mode is chosen", async () => {
    vi.mocked(authApi.state).mockResolvedValue({ mode: null, signed_in: false, username: null });
    renderGate();
    expect(await screen.findByRole("heading", { name: "Set up BACpack" })).toBeInTheDocument();
    expect(screen.queryByText("the app")).not.toBeInTheDocument();
  });

  it("shows Login when signed out in login mode", async () => {
    vi.mocked(authApi.state).mockResolvedValue({ mode: "login", signed_in: false, username: null });
    renderGate();
    expect(await screen.findByRole("heading", { name: "Sign in to BACpack" })).toBeInTheDocument();
    expect(screen.queryByText("the app")).not.toBeInTheDocument();
  });

  it("renders the app when mode is none", async () => {
    vi.mocked(authApi.state).mockResolvedValue({ mode: "none", signed_in: false, username: null });
    renderGate();
    expect(await screen.findByText("the app")).toBeInTheDocument();
  });

  it("renders the app when signed in", async () => {
    vi.mocked(authApi.state).mockResolvedValue({ mode: "login", signed_in: true, username: "admin" });
    renderGate();
    expect(await screen.findByText("the app")).toBeInTheDocument();
  });

  it("says so when the server can't be reached", async () => {
    vi.mocked(authApi.state).mockRejectedValue(new Error("network down"));
    renderGate();
    expect(await screen.findByText(/network down/)).toBeInTheDocument();
  });

  // The session ends while a form is open.
  it("keeps an open form's text when the session ends and the user signs in again", async () => {
    vi.mocked(authApi.state).mockResolvedValue({ mode: "login", signed_in: true, username: "admin" });
    vi.mocked(authApi.login).mockResolvedValue({ mode: "login", signed_in: true, username: "admin" });
    function Form() {
      const [text, setText] = useState("");
      return <input aria-label="description" value={text} onChange={(e) => setText(e.target.value)} />;
    }
    renderGate(<Form />);
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("description"), "half-written change");

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "login required" }), {
          status: 401,
          headers: { "content-type": "application/json" },
        })
      )
    );
    await api.get("/api/devices").catch(() => undefined);

    expect(await screen.findByRole("dialog", { name: "Your session has ended" })).toBeInTheDocument();
    expect(screen.getByLabelText("description")).toHaveValue("half-written change");

    await user.type(screen.getByLabelText("Username"), "admin");
    await user.type(screen.getByLabelText("Password"), "a long enough password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByLabelText("description")).toHaveValue("half-written change");
  });

  it("does not close the dialog on Escape", async () => {
    vi.mocked(authApi.state).mockResolvedValue({ mode: "login", signed_in: true, username: "admin" });
    renderGate();
    await screen.findByText("the app");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "login required" }), {
          status: 401,
          headers: { "content-type": "application/json" },
        })
      )
    );
    await api.get("/api/devices").catch(() => undefined);
    await screen.findByRole("dialog");
    await userEvent.keyboard("{Escape}");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("lands on the login page after sign-out even when the logout call fails", async () => {
    vi.mocked(authApi.state).mockResolvedValue({ mode: "login", signed_in: true, username: "admin" });
    vi.mocked(authApi.logout).mockRejectedValue(new Error("network down"));
    function SignOut() {
      const signOut = useSignOut();
      return <button onClick={() => void signOut()}>sign out</button>;
    }
    renderGate(<SignOut />);
    await userEvent.click(await screen.findByRole("button", { name: "sign out" }));
    expect(await screen.findByRole("heading", { name: "Sign in to BACpack" })).toBeInTheDocument();
  });

  // A poll already in flight when the user signs out answers 401 afterwards.
  it("does not say the session ended on a fresh sign-in after a late 401 from before sign-out", async () => {
    vi.mocked(authApi.state).mockResolvedValue({ mode: "login", signed_in: true, username: "admin" });
    vi.mocked(authApi.logout).mockResolvedValue(undefined as never);
    vi.mocked(authApi.login).mockResolvedValue({ mode: "login", signed_in: true, username: "admin" });
    function SignOut() {
      const signOut = useSignOut();
      return <button onClick={() => void signOut()}>sign out</button>;
    }
    renderGate(<SignOut />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "sign out" }));
    await screen.findByRole("heading", { name: "Sign in to BACpack" });

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "login required" }), {
          status: 401,
          headers: { "content-type": "application/json" },
        })
      )
    );
    await api.get("/api/devices").catch(() => undefined);

    await user.type(screen.getByLabelText("Username"), "admin");
    await user.type(screen.getByLabelText("Password"), "a long enough password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("button", { name: "sign out" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Your session has ended" })).not.toBeInTheDocument();
  });

  it("refetches the state when a call answers setup_required", async () => {
    vi.mocked(authApi.state).mockResolvedValue({ mode: "none", signed_in: false, username: null });
    renderGate();
    await screen.findByText("the app");
    vi.mocked(authApi.state).mockResolvedValue({ mode: null, signed_in: false, username: null });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "setup_required" }), {
          status: 403,
          headers: { "content-type": "application/json" },
        })
      )
    );
    await api.get("/api/devices").catch(() => undefined);
    expect(await screen.findByRole("heading", { name: "Set up BACpack" })).toBeInTheDocument();
  });
});

describe("AuthGate session dialog focus", () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(() => {
    setAuthHandlers({});
    vi.unstubAllGlobals();
    document.getElementById("overlay")?.remove();
  });

  async function expireSession() {
    vi.mocked(authApi.state).mockResolvedValue({ mode: "login", signed_in: true, username: "admin" });
    vi.mocked(authApi.login).mockResolvedValue({ mode: "login", signed_in: true, username: "admin" });
    const view = renderGate(
      <>
        <nav id="nav" />
        <main id="page">
          <input aria-label="behind" />
        </main>
      </>
    );
    await screen.findByLabelText("behind");
    // Stands in for EditOverlay, which renders under the body, not in the app.
    const overlay = document.createElement("div");
    overlay.id = "overlay";
    overlay.innerHTML = "<button>x</button>";
    document.body.appendChild(overlay);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "login required" }), {
          status: 401,
          headers: { "content-type": "application/json" },
        })
      )
    );
    await api.get("/api/devices").catch(() => undefined);
    const dialog = await screen.findByRole("dialog", { name: "Your session has ended" });
    return { view, dialog, overlay };
  }

  it("makes the page behind the dialog inert until the user signs in again", async () => {
    const { view, dialog } = await expireSession();
    expect(view.container).toHaveAttribute("inert");
    expect(dialog.closest("[inert]")).toBeNull();

    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Username"), "admin");
    await user.type(screen.getByLabelText("Password"), "a long enough password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(view.container).not.toHaveAttribute("inert");
  });

  it("makes an overlay rendered under the body inert too", async () => {
    const { overlay } = await expireSession();
    expect(overlay).toHaveAttribute("inert");
  });
});
