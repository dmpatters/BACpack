import { useState } from "react";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import ConfirmDialog, { useConfirm } from "./ConfirmDialog";

function Harness({ onConfirm = () => {}, busy = false }: { onConfirm?: () => void; busy?: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Delete device
      </button>
      {open && (
        <ConfirmDialog
          title="Delete device 599002?"
          confirmLabel="Delete device"
          busy={busy}
          onConfirm={onConfirm}
          onCancel={() => setOpen(false)}
        >
          This removes it.
        </ConfirmDialog>
      )}
    </>
  );
}

const confirmButton = () => screen.getAllByRole("button", { name: "Delete device" }).at(-1)!;

describe("ConfirmDialog", () => {
  it("opens with focus on Cancel and names itself", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Delete device" }));
    const dialog = screen.getByRole("alertdialog", { name: "Delete device 599002?" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAccessibleDescription("This removes it.");
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
  });

  it("keeps Tab inside the dialog", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Delete device" }));
    await user.tab();
    expect(confirmButton()).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
    await user.tab({ shift: true });
    expect(confirmButton()).toHaveFocus();
  });

  it("cancels on Escape and gives focus back to the opener", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const opener = screen.getByRole("button", { name: "Delete device" });
    await user.click(opener);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it("cancels on a scrim click but not on a click inside", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Delete device" }));
    await user.click(screen.getByText("This removes it."));
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    await user.click(screen.getByTestId("confirm-scrim"));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("confirms once, and not while busy", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    const { rerender } = render(<Harness onConfirm={onConfirm} />);
    await user.click(screen.getByRole("button", { name: "Delete device" }));
    await user.click(confirmButton());
    expect(onConfirm).toHaveBeenCalledTimes(1);
    rerender(<Harness onConfirm={onConfirm} busy />);
    expect(confirmButton()).toBeDisabled();
  });

  // The action is already under way: closing would look like an abort.
  it("can't be cancelled while busy", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Delete device" }));
    rerender(<Harness busy />);
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    await user.keyboard("{Escape}");
    await user.click(screen.getByTestId("confirm-scrim"));
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
  });

  it("keeps focus inside while the action runs", async () => {
    const user = userEvent.setup();
    const { rerender } = render(
      <ConfirmDialog title="Delete AHU?" confirmLabel="Delete device" onConfirm={() => {}} onCancel={() => {}}>
        Its 55 objects go with it.
      </ConfirmDialog>
    );
    rerender(
      <ConfirmDialog title="Delete AHU?" confirmLabel="Delete device" busy onConfirm={() => {}} onCancel={() => {}}>
        Its 55 objects go with it.
      </ConfirmDialog>
    );
    const dialog = screen.getByRole("alertdialog");
    expect(dialog).toHaveFocus();
    await user.tab();
    expect(dialog).toHaveFocus();
  });
});

function AskHarness({ onConfirm }: { onConfirm: () => Promise<unknown> }) {
  const { ask, dialog } = useConfirm();
  return (
    <>
      <button type="button" onClick={() => ask({ title: "Delete AV-1?", body: "Gone for good.", confirmLabel: "Delete object", onConfirm })}>
        Delete
      </button>
      {dialog}
    </>
  );
}

describe("useConfirm", () => {
  it("runs the action once, busy until it finishes, then closes", async () => {
    const user = userEvent.setup();
    let finish!: () => void;
    const onConfirm = vi.fn(() => new Promise<void>((resolve) => (finish = resolve)));
    render(<AskHarness onConfirm={onConfirm} />);
    await user.click(screen.getByRole("button", { name: "Delete" }));
    await user.click(screen.getByRole("button", { name: "Delete object" }));
    expect(screen.getByRole("button", { name: "Delete object" })).toBeDisabled();
    await act(async () => finish());
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("closes when the action fails, leaving the error to the caller", async () => {
    const user = userEvent.setup();
    render(<AskHarness onConfirm={() => Promise.reject(new Error("HTTP 409"))} />);
    await user.click(screen.getByRole("button", { name: "Delete" }));
    await user.click(screen.getByRole("button", { name: "Delete object" }));
    expect(await screen.findByRole("button", { name: "Delete" })).toBeInTheDocument();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("does nothing on Cancel", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn(() => Promise.resolve());
    render(<AskHarness onConfirm={onConfirm} />);
    await user.click(screen.getByRole("button", { name: "Delete" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
