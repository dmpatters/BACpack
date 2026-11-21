import type { ReactNode } from "react";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { ClipCell, RowActions, SortHeader, TableFrame } from "./DataTable";

function renderTable(children: ReactNode, scroll = false) {
  return render(
    <MemoryRouter>
      <TableFrame label="Things" scroll={scroll}>
        <tbody>
          <tr>{children}</tr>
        </tbody>
      </TableFrame>
    </MemoryRouter>
  );
}

describe("TableFrame", () => {
  it("names the table and uses the design-system classes", () => {
    renderTable(<td>x</td>);
    const table = screen.getByRole("table", { name: "Things" });
    expect(table).toHaveClass("bp-table");
    expect(table.parentElement).toHaveClass("bp-table-wrap");
  });

  it("makes a scrolling frame a focusable, named region", () => {
    renderTable(<td>x</td>, true);
    const region = screen.getByRole("region", { name: "Things" });
    expect(region).toHaveClass("bp-table-wrap", "bp-table-wrap--scroll");
    expect(region).toHaveAttribute("tabindex", "0");
  });
});

describe("SortHeader", () => {
  function Header({ sort, onSort }: { sort: { key: "name"; dir: "ascending" | "descending" } | null; onSort: (k: "name") => void }) {
    return (
      <table>
        <thead>
          <tr>
            <SortHeader label="Name" sortKey="name" sort={sort} onSort={onSort} />
          </tr>
        </thead>
      </table>
    );
  }

  it("has no aria-sort until sorted, then says which way", () => {
    const { rerender } = render(<Header sort={null} onSort={() => {}} />);
    expect(screen.getByRole("columnheader")).not.toHaveAttribute("aria-sort");
    rerender(<Header sort={{ key: "name", dir: "descending" }} onSort={() => {}} />);
    expect(screen.getByRole("columnheader")).toHaveAttribute("aria-sort", "descending");
  });

  it("sorts from the keyboard", async () => {
    const user = userEvent.setup();
    const onSort = vi.fn();
    render(<Header sort={null} onSort={onSort} />);
    await user.tab();
    expect(screen.getByRole("button", { name: "Name" })).toHaveFocus();
    expect(screen.getByRole("button", { name: "Name" })).toHaveClass("bp-sort");
    await user.keyboard("{Enter}");
    expect(onSort).toHaveBeenCalledWith("name");
  });
});

describe("ClipCell", () => {
  it("clips a long value and keeps all of it in the title", () => {
    const url = "http://example.com/a/very/long/path?with=a&query=string";
    renderTable(<ClipCell value={url} />);
    const cell = screen.getByRole("cell");
    expect(cell).toHaveClass("bp-cell-clip");
    expect(cell).toHaveAttribute("title", url);
  });

  it("shows a muted em dash when empty", () => {
    renderTable(<ClipCell value={null} />);
    expect(screen.getByRole("cell")).toHaveTextContent("—");
    expect(screen.getByRole("cell")).not.toHaveClass("bp-cell-clip");
  });
});

describe("RowActions", () => {
  it("is an Edit link then a quiet Delete", async () => {
    const user = userEvent.setup();
    const onDelete = vi.fn();
    renderTable(<RowActions editTo="/things/1" onDelete={onDelete} />);
    const cell = screen.getByRole("cell");
    expect(cell).toHaveClass("bp-cell-actions");
    const edit = within(cell).getByRole("link", { name: "Edit" });
    const del = within(cell).getByRole("button", { name: "Delete" });
    expect(edit).toHaveAttribute("href", "/things/1");
    expect(edit.compareDocumentPosition(del) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(del).toHaveClass("bp-btn--delete", "bp-btn--sm");
    await user.click(del);
    expect(onDelete).toHaveBeenCalled();
  });

  it("offers an Edit button where there is no page to link to", async () => {
    const user = userEvent.setup();
    const onEdit = vi.fn();
    renderTable(<RowActions onEdit={onEdit} onDelete={() => {}} />);
    await user.click(screen.getByRole("button", { name: "Edit" }));
    expect(onEdit).toHaveBeenCalled();
  });
});
