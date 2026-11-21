import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import ApiDocs from "./ApiDocs";
import { mountSwagger } from "./apiDocsSwagger";

vi.mock("./apiDocsSwagger", () => ({ mountSwagger: vi.fn() }));

describe("ApiDocs", () => {
  it("mounts Swagger on the local OpenAPI document", async () => {
    render(
      <MemoryRouter>
        <ApiDocs />
      </MemoryRouter>
    );
    await waitFor(() => expect(mountSwagger).toHaveBeenCalledWith(expect.any(HTMLElement), "/openapi.json"));
    expect(document.title).toBe("API Docs · BACpack");
    expect(screen.getByRole("heading", { name: "API Docs" })).toBeInTheDocument();
  });
  it("links the OpenAPI description in the page intro", () => {
    render(
      <MemoryRouter>
        <ApiDocs />
      </MemoryRouter>
    );
    expect(screen.getByRole("link", { name: "Download the OpenAPI description (JSON)" })).toHaveAttribute(
      "href",
      "/openapi.json"
    );
  });
  it("says so when Swagger fails to load", async () => {
    vi.mocked(mountSwagger).mockImplementationOnce(() => {
      throw new Error("boom");
    });
    render(
      <MemoryRouter>
        <ApiDocs />
      </MemoryRouter>
    );
    expect(await screen.findByText(/Couldn't load the API docs/)).toBeInTheDocument();
  });
});
