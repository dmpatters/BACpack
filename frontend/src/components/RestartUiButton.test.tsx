import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import RestartUiButton from "./RestartUiButton";
import { engineApi, versionApi } from "../api/endpoints";

vi.mock("../api/endpoints", () => ({
  engineApi: { restartUi: vi.fn() },
  versionApi: { get: vi.fn() },
}));

function renderButton(onNotice = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <RestartUiButton onNotice={onNotice} />
    </QueryClientProvider>
  );
  return onNotice;
}

describe("RestartUiButton", () => {
  beforeEach(() => vi.clearAllMocks());

  it("is hidden when no service manager runs BACpack", async () => {
    vi.mocked(versionApi.get).mockResolvedValue({ version: "0.32.0", service: false });
    renderButton();
    await screen.findByTestId("restart-ui-slot");
    expect(screen.queryByRole("button", { name: "Restart UI only" })).toBeNull();
  });

  it("shows a custom label", async () => {
    vi.mocked(versionApi.get).mockResolvedValue({ version: "0.43.0", service: true });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <RestartUiButton onNotice={vi.fn()} label="Restart web server" />
      </QueryClientProvider>
    );
    expect(await screen.findByRole("button", { name: "Restart web server" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Restart UI only" })).toBeNull();
  });

  it("restarts the UI and says devices keep running", async () => {
    vi.mocked(versionApi.get).mockResolvedValue({ version: "0.32.0", service: true });
    vi.mocked(engineApi.restartUi).mockResolvedValue({ restarting: true });
    const onNotice = renderButton();
    await userEvent.setup().click(await screen.findByRole("button", { name: "Restart UI only" }));
    expect(engineApi.restartUi).toHaveBeenCalledTimes(1);
    expect(onNotice).toHaveBeenLastCalledWith({
      type: "ok",
      message: "the web UI is restarting. Reload in a few seconds.",
    });
  });
});
