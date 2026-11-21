import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, RouterProvider, Routes, createMemoryRouter, useParams } from "react-router-dom";
import { describe, expect, it } from "vitest";
import Tabs, { TabPanels } from "./Tabs";

const TABS = [
  { slug: "first", label: "First" },
  { slug: "second", label: "Second" },
];

function Draft() {
  const [value, setValue] = useState("");
  return <input aria-label="Draft" value={value} onChange={(e) => setValue(e.target.value)} />;
}

function Page() {
  const { tab = "first" } = useParams();
  return (
    <>
      <Tabs tabs={TABS} active={tab} basePath="/thing/1" label="Thing sections" />
      <TabPanels
        active={tab}
        panels={[
          { key: "first", render: () => <Draft /> },
          { key: "second", render: () => <p>Second panel</p> },
        ]}
      />
    </>
  );
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/thing/:id/:tab?" element={<Page />} />
      </Routes>
    </MemoryRouter>
  );
}

describe("Tabs", () => {
  it("links each tab to its URL and marks the current one", () => {
    renderAt("/thing/1/second");
    const nav = screen.getByRole("navigation", { name: "Thing sections" });
    expect(nav).toHaveClass("bp-tabs");
    expect(screen.getByRole("link", { name: "First" })).toHaveAttribute("href", "/thing/1");
    expect(screen.getByRole("link", { name: "Second" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByText("Second panel")).toBeVisible();
  });

  it("keeps a typed draft across a tab round trip", async () => {
    const user = userEvent.setup();
    renderAt("/thing/1");
    await user.type(screen.getByLabelText("Draft"), "unsaved");
    await user.click(screen.getByRole("link", { name: "Second" }));
    expect(screen.getByText("Second panel")).toBeVisible();
    await user.click(screen.getByRole("link", { name: "First" }));
    expect(screen.getByLabelText("Draft")).toHaveValue("unsaved");
  });

  it("replaces the history entry when asked, keeping the state", async () => {
    const user = userEvent.setup();
    const router = createMemoryRouter(
      [
        {
          path: "/thing/:id/:tab?",
          element: <Tabs tabs={TABS} active="first" basePath="/thing/1" label="Thing sections" replace state={{ keep: 1 }} />,
        },
      ],
      { initialEntries: ["/thing/1"] }
    );
    render(<RouterProvider router={router} />);
    await user.click(screen.getByRole("link", { name: "Second" }));
    expect(router.state.location.pathname).toBe("/thing/1/second");
    expect(router.state.location.state).toEqual({ keep: 1 });
    expect(router.state.historyAction).toBe("REPLACE");
  });

  it("pushes by default", async () => {
    const user = userEvent.setup();
    const router = createMemoryRouter(
      [
        {
          path: "/thing/:id/:tab?",
          element: <Tabs tabs={TABS} active="first" basePath="/thing/1" label="Thing sections" />,
        },
      ],
      { initialEntries: ["/thing/1"] }
    );
    render(<RouterProvider router={router} />);
    await user.click(screen.getByRole("link", { name: "Second" }));
    expect(router.state.historyAction).toBe("PUSH");
  });

  it("doesn't claim tab-panel semantics for its panels", () => {
    const { container } = renderAt("/thing/1/second");
    expect(container.querySelector('[role="tabpanel"]')).toBeNull();
  });
});
