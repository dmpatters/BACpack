import { render, renderHook, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import PageHeader from "./PageHeader";
import Callout from "./Callout";
import Banner, { LoadError } from "./Banner";
import { usePageTitle } from "../utils/usePageTitle";

describe("PageHeader", () => {
  it("renders one h1 with the meta, the subtitle and the actions", () => {
    render(
      <PageHeader
        title="Bugfix Session Device"
        meta="device 599002"
        sub="5 of every object type"
        actions={<button type="button">Delete device</button>}
      />
    );
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Bugfix Session Devicedevice 599002");
    expect(screen.getByText("5 of every object type")).toHaveClass("bp-page-header__sub");
    expect(screen.getByRole("button", { name: "Delete device" })).toBeInTheDocument();
  });
});

describe("usePageTitle", () => {
  it("joins the parts most specific first and ends with BACpack", () => {
    renderHook(() => usePageTitle("Bugfix Session Device", undefined, "Objects"));
    expect(document.title).toBe("Bugfix Session Device · Objects · BACpack");
  });
});

describe("Callout", () => {
  it.each([
    ["info", "note"],
    ["ok", "status"],
    ["warn", "alert"],
    ["danger", "alert"],
  ] as const)("a %s callout has role %s", (tone, role) => {
    render(
      <Callout tone={tone} lead="Lead.">
        Text
      </Callout>
    );
    expect(screen.getByRole(role)).toHaveClass("bp-callout", `bp-callout--${tone}`);
  });

  it("Banner renders a callout and LoadError an error block", () => {
    render(
      <>
        <Banner notice={{ type: "ok", message: "saved" }} />
        <LoadError what="devices" error={new Error("boom")} />
      </>
    );
    expect(screen.getByRole("status")).toHaveTextContent("saved");
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't load devices: boom");
    expect(screen.getByRole("alert")).toHaveClass("bp-error");
  });
});
