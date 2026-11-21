import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ConnectorPage from "./ConnectorPage";
import { connectorsApi } from "../api/endpoints";
import { ApiError } from "../api/http";
import type { ConnectorDTO } from "../api/types";

vi.mock("../api/endpoints", () => ({ connectorsApi: { get: vi.fn() } }));
vi.mock("./BacnetConnectorDetail", () => ({ default: () => <p>bacnet detail</p> }));
vi.mock("./ConnectorDetail", () => ({ default: () => <p>http detail</p> }));

function renderPage(id: number | string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/connectors/${id}`]}>
        <Routes>
          <Route path="/connectors/:connectorId" element={<ConnectorPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe("ConnectorPage", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows the detail page for the connector's type", async () => {
    vi.mocked(connectorsApi.get).mockResolvedValue({ id: 7, type: "bacnet" } as ConnectorDTO);
    renderPage(7);
    expect(await screen.findByText("bacnet detail")).toBeInTheDocument();
  });

  it("says the connector wasn't found on a 404", async () => {
    vi.mocked(connectorsApi.get).mockRejectedValue(new ApiError("Connector 99 not found", 404));
    renderPage(99);
    expect(await screen.findByRole("heading", { name: "Connector 99 not found" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to connectors" })).toHaveAttribute("href", "/connectors");
    expect(document.title).toBe("Connector 99 not found · BACpack");
  });

  it("says why when the connector can't be loaded for another reason", async () => {
    vi.mocked(connectorsApi.get).mockRejectedValue(new Error("Failed to fetch"));
    renderPage(99);
    expect(await screen.findByRole("heading", { name: "Couldn't load connector 99" })).toBeInTheDocument();
    expect(screen.getByText("Failed to fetch")).toBeInTheDocument();
  });

  it("says a non-numeric connector id isn't found, without asking the server", async () => {
    renderPage("abc");
    expect(await screen.findByRole("heading", { name: "Connector abc not found" })).toBeInTheDocument();
    expect(connectorsApi.get).not.toHaveBeenCalled();
  });
});
