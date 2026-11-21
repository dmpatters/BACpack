import { useState } from "react";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import TableToolbar, { countText } from "./TableToolbar";

describe("countText", () => {
  it("counts with real plurals, and says when a search narrows it", () => {
    expect(countText(55, 55, "object")).toBe("55 objects");
    expect(countText(1, 1, "object")).toBe("1 object");
    expect(countText(12, 55, "object")).toBe("12 of 55 objects");
    expect(countText(2, 2, "entry", "entries")).toBe("2 entries");
  });
});

function Harness({ selected = 0 }: { selected?: number }) {
  const [search, setSearch] = useState("");
  const [type, setType] = useState("");
  return (
    <>
      <TableToolbar
        search={search}
        onSearch={setSearch}
        searchLabel="Search objects"
        filter={{ label: "Object type", value: type, onChange: setType, options: [{ value: "", label: "All types" }, { value: "analog-value", label: "analog-value" }] }}
        count="55 objects"
        selectedCount={selected}
        bulkActions={<button type="button">Edit selected</button>}
        onClearSelection={vi.fn()}
      />
      <table aria-label="Objects" />
      <output>{`${search}|${type}`}</output>
    </>
  );
}

describe("TableToolbar", () => {
  it("searches and filters", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const searchBox = screen.getByRole("searchbox", { name: "Search objects" });
    expect(searchBox).toHaveClass("bp-input", "bp-toolbar__search");
    await user.type(searchBox, "zone");
    await user.selectOptions(screen.getByRole("combobox", { name: "Object type" }), "analog-value");
    expect(screen.getByRole("status")).toHaveTextContent("zone|analog-value");
  });

  it("shows the count until rows are selected, then the bulk bar in its place", () => {
    const { rerender } = render(<Harness />);
    const toolbar = screen.getByRole("searchbox").parentElement!;
    const table = screen.getByRole("table", { name: "Objects" });
    expect(within(toolbar).getByText("55 objects")).toHaveClass("bp-toolbar__count");
    expect(toolbar.nextElementSibling).toBe(table);

    rerender(<Harness selected={3} />);
    const bar = within(toolbar).getByRole("group", { name: "Bulk actions" });
    expect(bar).toHaveClass("bp-bulkbar");
    expect(within(bar).getByText("3 selected")).toHaveClass("bp-bulkbar__count");
    expect(within(bar).getByRole("button", { name: "Edit selected" })).toBeInTheDocument();
    expect(within(bar).getByRole("button", { name: "Clear selection" })).toBeInTheDocument();
    expect(within(toolbar).queryByText("55 objects")).not.toBeInTheDocument();
    // Same toolbar, same next sibling: the table didn't move down a row.
    expect(toolbar.nextElementSibling).toBe(table);
  });
});
