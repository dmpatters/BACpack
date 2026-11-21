import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ActionStatus, ErrorBlock, Loading } from "./AsyncState";
import { LoadError } from "./Banner";

const idle = { isError: false, isSuccess: false, error: null };

describe("Loading", () => {
  afterEach(() => vi.useRealTimers());

  it("says nothing for the first 300 ms, then shows a spinner with words", async () => {
    vi.useFakeTimers();
    render(<Loading what="objects" />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(300));
    const status = screen.getByRole("status");
    expect(status).toHaveClass("bp-loading");
    expect(status).toHaveTextContent("Loading objects…");
    expect(status.querySelector(".bp-spinner")).toHaveAttribute("aria-hidden", "true");
  });
});

describe("ErrorBlock and LoadError", () => {
  it("leads with what failed, then the reason, with a Retry", async () => {
    const retry = vi.fn();
    render(<LoadError what="devices" error={new Error("HTTP 500: database is locked")} onRetry={retry} />);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveClass("bp-error");
    expect(alert).toHaveTextContent("Couldn't load devices: HTTP 500: database is locked");
    expect(alert.querySelector("strong")).toHaveTextContent("Couldn't load devices:");
    await userEvent.setup().click(screen.getByRole("button", { name: "Retry" }));
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it("has no Retry when there's nothing to retry", () => {
    render(<ErrorBlock lead="Can't save:">Every remote write target needs a device instance.</ErrorBlock>);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});

describe("ActionStatus", () => {
  it("shows nothing before the action ran", () => {
    const { container } = render(<ActionStatus action={idle} failed="Couldn't save" done="Saved." />);
    expect(container).toBeEmptyDOMElement();
  });

  it("puts the error under the action, leading with what failed", () => {
    render(<ActionStatus action={{ ...idle, isError: true, error: new Error("name already used") }} failed="Couldn't save" />);
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't save: name already used");
  });

  it("confirms a success, and hides it again once the form is edited", () => {
    const done = { ...idle, isSuccess: true };
    const { rerender } = render(<ActionStatus action={done} failed="Couldn't save" done="Saved." />);
    expect(screen.getByRole("status")).toHaveTextContent("Saved.");
    expect(screen.getByRole("status")).toHaveClass("bp-done");
    rerender(<ActionStatus action={done} failed="Couldn't save" done="Saved." hideDone />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
