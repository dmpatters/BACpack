import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Link, MemoryRouter, Route, RouterProvider, Routes, createMemoryRouter, useNavigate } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { LEAVE_UNSAVED, LeaveGuard, UnsavedChangesProvider, differs, rebase, staysOnPage, useUnsavedChanges } from "./UnsavedChanges";

function Draft() {
  const [name, setName] = useState("AHU");
  useUnsavedChanges(name !== "AHU", "/devices/1");
  const navigate = useNavigate();
  return (
    <>
      <label htmlFor="name">Name</label>
      <input id="name" value={name} onChange={(e) => setName(e.target.value)} />
      <Link to="/devices/1/objects">Objects tab</Link>
      <Link to="/devices/1/objects/5">An object</Link>
      <Link to="/logs">Logs</Link>
      <button type="button" onClick={() => navigate("/", { state: LEAVE_UNSAVED })}>
        Deleted, go home
      </button>
    </>
  );
}

function renderGuarded(path = "/devices/1") {
  const router = createMemoryRouter(
    [
      {
        path: "*",
        element: (
          <UnsavedChangesProvider>
            <Routes>
              <Route path="/devices/:id/:tab?" element={<Draft />} />
              <Route path="*" element={<p>somewhere else</p>} />
            </Routes>
            <LeaveGuard />
          </UnsavedChangesProvider>
        ),
      },
    ],
    { initialEntries: [path] }
  );
  render(<RouterProvider router={router} />);
  return router;
}

describe("staysOnPage", () => {
  it("counts the page and its tabs as staying, anything deeper or elsewhere as leaving", () => {
    expect(staysOnPage("/devices/1", "/devices/1")).toBe(true);
    expect(staysOnPage("/devices/1", "/devices/1/settings")).toBe(true);
    expect(staysOnPage("/devices/1", "/devices/1/objects/5")).toBe(true);
    expect(staysOnPage("/devices/1", "/devices/1/objects/5/trending")).toBe(true);
    expect(staysOnPage("/devices/1", "/devices/1/process/x")).toBe(false);
    expect(staysOnPage("/devices/1/objects/5", "/devices/1/objects/5/trending")).toBe(true);
    expect(staysOnPage("/devices/1/objects/5", "/devices/1")).toBe(false);
    expect(staysOnPage("/devices/1/objects/5", "/devices/1/objects/6")).toBe(false);
    expect(staysOnPage("/devices/1", "/devices/10")).toBe(false);
    expect(staysOnPage("/devices/1", "/logs")).toBe(false);
  });
});

describe("differs", () => {
  it("compares values, not identity", () => {
    expect(differs({ a: [1, 2] }, { a: [1, 2] })).toBe(false);
    expect(differs({ a: "x" }, { a: "y" })).toBe(true);
  });
});

describe("LeaveGuard", () => {
  it("asks before a link drops an edit, and keeps the draft on Cancel", async () => {
    const user = userEvent.setup();
    const router = renderGuarded();
    await user.type(screen.getByLabelText("Name"), " 2");
    await user.click(screen.getByRole("link", { name: "Logs" }));
    expect(screen.getByRole("alertdialog", { name: "Discard unsaved changes?" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(router.state.location.pathname).toBe("/devices/1");
    expect(screen.getByLabelText("Name")).toHaveValue("AHU 2");
  });

  it("leaves when told to discard", async () => {
    const user = userEvent.setup();
    const router = renderGuarded();
    await user.type(screen.getByLabelText("Name"), " 2");
    await user.click(screen.getByRole("link", { name: "Logs" }));
    await user.click(screen.getByRole("button", { name: "Discard changes" }));
    expect(router.state.location.pathname).toBe("/logs");
  });

  it("doesn't ask when a device page's edit stays mounted under an object's overlay", async () => {
    const user = userEvent.setup();
    const router = renderGuarded();
    await user.type(screen.getByLabelText("Name"), " 2");
    await user.click(screen.getByRole("link", { name: "An object" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/devices/1/objects/5");
  });

  it("doesn't ask when the form is clean", async () => {
    const user = userEvent.setup();
    const router = renderGuarded();
    await user.click(screen.getByRole("link", { name: "Logs" }));
    expect(router.state.location.pathname).toBe("/logs");
  });

  it("doesn't ask for a tab of the same page, or after a confirmed delete", async () => {
    const user = userEvent.setup();
    const router = renderGuarded();
    await user.type(screen.getByLabelText("Name"), " 2");
    await user.click(screen.getByRole("link", { name: "Objects tab" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/devices/1/objects");
    await user.click(screen.getByRole("button", { name: "Deleted, go home" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/");
  });

  it("asks the browser before a reload or close drops an edit", async () => {
    const user = userEvent.setup();
    renderGuarded();
    const clean = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(clean);
    expect(clean.defaultPrevented).toBe(false);
    await user.type(screen.getByLabelText("Name"), " 2");
    const dirty = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(dirty);
    expect(dirty.defaultPrevented).toBe(true);
  });
});

describe("useUnsavedChanges", () => {
  it("does nothing without a provider, so forms render in plain tests", () => {
    render(
      <MemoryRouter initialEntries={["/devices/1"]}>
        <Draft />
      </MemoryRouter>
    );
    expect(screen.getByLabelText("Name")).toHaveValue("AHU");
  });
});

describe("rebase", () => {
  it("keeps the fields edited since the baseline and takes the fresh value for the rest", () => {
    const baseline = { name: "AHU", description: "old", location: "" };
    const form = { name: "AHU", description: "typed", location: "" };
    const fresh = { name: "AHU-2", description: "old", location: "Roof" };
    expect(rebase(form, baseline, fresh)).toEqual({ name: "AHU-2", description: "typed", location: "Roof" });
  });
});
