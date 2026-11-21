import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, RouterProvider, Routes, createMemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import WebServer from "./WebServer";
import { engineApi, versionApi, webServerApi } from "../api/endpoints";
import type { WebServerSettings } from "../api/types";
import { LeaveGuard, UnsavedChangesProvider } from "../components/UnsavedChanges";
import { inSettingsColumn } from "../testing/columns";

vi.mock("../api/endpoints", () => ({
  WEB_CERTIFICATE_URL: "/api/settings/web/certificate.pem",
  webServerApi: { get: vi.fn(), setListenAddress: vi.fn(), useSelfSigned: vi.fn(), uploadCertificate: vi.fn() },
  versionApi: { get: vi.fn() },
  engineApi: { restartUi: vi.fn() },
}));

const FINGERPRINT = "AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89";

function settings(overrides: Partial<WebServerSettings> = {}): WebServerSettings {
  return {
    listen_address: "0.0.0.0",
    running: { address: "0.0.0.0", port: 8000, fingerprint: FINGERPRINT },
    certificate: {
      fingerprint_sha256: FINGERPRINT,
      subject: "O=BACpack self-signed,CN=bacbox",
      issuer: "O=BACpack self-signed,CN=bacbox",
      not_before: "2026-11-07T10:00:00+00:00",
      not_after: "2036-11-04T10:00:00+00:00",
      sans: ["bacbox", "localhost", "127.0.0.1", "10.0.0.5"],
      source: "self-signed",
      expired: false,
    },
    certificate_problem: null,
    restart_pending: false,
    ...overrides,
  };
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <UnsavedChangesProvider>
          <WebServer />
        </UnsavedChangesProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
  return userEvent.setup();
}

describe("Web server page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(webServerApi.get).mockResolvedValue(settings());
    vi.mocked(versionApi.get).mockResolvedValue({ version: "0.43.0", service: true, listen_address: "0.0.0.0" });
  });

  it("says Listen address once, and puts the address and certificate panels in the settings column", async () => {
    renderPage();
    await screen.findByLabelText("Address");
    expect(screen.queryAllByText("Listen address")).toHaveLength(1);
    expect(screen.getByRole("heading", { name: "Web Server" })).toBeInTheDocument();
    for (const name of ["Listen address", "Certificate", "Use your own certificate"]) {
      expect(inSettingsColumn(screen.getByRole("group", { name }))).toBe(true);
    }
    expect(inSettingsColumn(screen.getByRole("heading", { name: "Web Server" }))).toBe(false);
  });

  it("shows the listen address, the fingerprint and the dates with their zone", async () => {
    renderPage();
    expect(await screen.findByLabelText("Address")).toHaveValue("0.0.0.0");
    expect(screen.getByText(FINGERPRINT)).toBeInTheDocument();
    expect(screen.getByText("2026-11-07 10:00:00 UTC")).toBeInTheDocument();
    expect(screen.getByText("2036-11-04 10:00:00 UTC")).toBeInTheDocument();
    expect(screen.getByText("bacbox, localhost, 127.0.0.1, 10.0.0.5")).toBeInTheDocument();
    expect(screen.getByText("BACpack self-signed")).toBeInTheDocument();
  });

  it("saving the listen address calls setListenAddress and shows the server's refusal under the field", async () => {
    vi.mocked(webServerApi.setListenAddress).mockRejectedValue(new Error("This machine does not have the address 192.0.2.9."));
    const user = renderPage();
    const field = await screen.findByLabelText("Address");
    await user.clear(field);
    await user.type(field, "192.0.2.9");
    await user.click(screen.getByRole("button", { name: "Save address" }));
    expect(webServerApi.setListenAddress).toHaveBeenCalledWith("192.0.2.9");
    expect(await screen.findByText("This machine does not have the address 192.0.2.9.", { selector: "[role=alert]" })).toBeInTheDocument();
    expect(field).toHaveAccessibleDescription("This machine does not have the address 192.0.2.9.");
  });

  it("shows the saved value in the field after a save, trimmed", async () => {
    vi.mocked(webServerApi.setListenAddress).mockResolvedValue(settings({ listen_address: "10.0.0.5" }));
    const user = renderPage();
    const field = await screen.findByLabelText("Address");
    await user.clear(field);
    await user.type(field, " 10.0.0.5 ");
    await user.click(screen.getByRole("button", { name: "Save address" }));
    expect(webServerApi.setListenAddress).toHaveBeenCalledWith("10.0.0.5");
    await vi.waitFor(() => expect(field).toHaveValue("10.0.0.5"));
    expect(screen.getByRole("button", { name: "Save address" })).toBeDisabled();
  });

  it("follows a changed server value while untouched, and keeps an edit", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <UnsavedChangesProvider>
            <WebServer />
          </UnsavedChangesProvider>
        </MemoryRouter>
      </QueryClientProvider>
    );
    const user = userEvent.setup();
    const field = await screen.findByLabelText("Address");
    await act(async () => client.setQueryData(["webServer"], settings({ listen_address: "10.0.0.5" })));
    await vi.waitFor(() => expect(field).toHaveValue("10.0.0.5"));
    await user.type(field, "1");
    await act(async () => client.setQueryData(["webServer"], settings({ listen_address: "127.0.0.1" })));
    expect(field).toHaveValue("10.0.0.51");
  });

  it("warns that the browser will lose the page, and names the URL, before a restart onto another address", async () => {
    vi.mocked(webServerApi.get).mockResolvedValue(settings({ listen_address: "10.0.0.5", restart_pending: true }));
    renderPage();
    expect(await screen.findByText(/will lose the page unless localhost points at 10\.0\.0\.5/)).toBeInTheDocument();
    expect(screen.getByText("https://10.0.0.5:8000")).toBeInTheDocument();
  });

  it("does not warn when the running address is already the saved one (only the certificate changed)", async () => {
    vi.mocked(webServerApi.get).mockResolvedValue(
      settings({
        listen_address: "10.0.0.5",
        restart_pending: true,
        running: { address: "10.0.0.5", port: 8000, fingerprint: FINGERPRINT },
      })
    );
    renderPage();
    expect(await screen.findByText(/Restart the web server to apply/)).toBeInTheDocument();
    expect(screen.queryByText(/will lose the page/)).not.toBeInTheDocument();
  });

  it.each(["0.0.0.0", "localhost"])("does not warn when the saved address is %s", async (address) => {
    vi.mocked(webServerApi.get).mockResolvedValue(settings({ listen_address: address, restart_pending: true }));
    renderPage();
    expect(await screen.findByText(/Restart the web server to apply/)).toBeInTheDocument();
    expect(screen.queryByText(/will lose the page/)).not.toBeInTheDocument();
  });

  it("shows the restart callout and the Restart web server button only when a restart is pending and the version says service", async () => {
    renderPage();
    await screen.findByLabelText("Address");
    expect(screen.queryByText(/Restart the web server to apply/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Restart web server" })).not.toBeInTheDocument();
  });

  it("shows the restart button when a restart is pending under a service manager", async () => {
    vi.mocked(webServerApi.get).mockResolvedValue(settings({ restart_pending: true }));
    vi.mocked(engineApi.restartUi).mockResolvedValue({ restarting: true });
    const user = renderPage();
    expect(await screen.findByText(/Restart the web server to apply/)).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: "Restart web server" }));
    expect(engineApi.restartUi).toHaveBeenCalledTimes(1);
  });

  it("hides the restart button when no service manager runs BACpack", async () => {
    vi.mocked(webServerApi.get).mockResolvedValue(settings({ restart_pending: true }));
    vi.mocked(versionApi.get).mockResolvedValue({ version: "0.43.0", service: false, listen_address: null });
    renderPage();
    expect(await screen.findByText(/Restart the web server to apply/)).toBeInTheDocument();
    await screen.findByTestId("restart-ui-slot");
    expect(screen.queryByRole("button", { name: "Restart web server" })).not.toBeInTheDocument();
  });

  it("Regenerate asks for confirmation before posting", async () => {
    vi.mocked(webServerApi.useSelfSigned).mockResolvedValue(settings());
    const user = renderPage();
    await user.click(await screen.findByRole("button", { name: "Regenerate" }));
    expect(webServerApi.useSelfSigned).not.toHaveBeenCalled();
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText(/Browsers will warn again/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Regenerate" }));
    expect(webServerApi.useSelfSigned).toHaveBeenCalledTimes(1);
  });

  it("shows Go back to self-signed for an own certificate", async () => {
    const own = settings();
    own.certificate = { ...own.certificate!, source: "own", subject: "CN=mine", issuer: "CN=ca" };
    vi.mocked(webServerApi.get).mockResolvedValue(own);
    renderPage();
    expect(await screen.findByRole("button", { name: "Go back to self-signed" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Regenerate" })).not.toBeInTheDocument();
    expect(screen.getByText("Your own certificate")).toBeInTheDocument();
  });

  it("Install certificate stays disabled until both files are chosen, then posts their text", async () => {
    vi.mocked(webServerApi.uploadCertificate).mockResolvedValue(settings());
    const user = renderPage();
    const install = await screen.findByRole("button", { name: "Install certificate" });
    expect(install).toBeDisabled();
    await user.upload(screen.getByLabelText("Certificate (PEM, chain allowed)"), new File(["CERT TEXT"], "c.pem"));
    expect(install).toBeDisabled();
    await user.upload(screen.getByLabelText("Private key (PEM)"), new File(["KEY TEXT"], "k.pem"));
    expect(install).toBeEnabled();
    await user.click(install);
    await waitFor(() =>
      expect(webServerApi.uploadCertificate).toHaveBeenCalledWith({ certificate_pem: "CERT TEXT", key_pem: "KEY TEXT" })
    );
  });

  it("an upload refusal is shown and the form keeps its files", async () => {
    vi.mocked(webServerApi.uploadCertificate).mockRejectedValue(new Error("The private key does not match the certificate."));
    const user = renderPage();
    await user.upload(await screen.findByLabelText("Certificate (PEM, chain allowed)"), new File(["C"], "c.pem"));
    await user.upload(screen.getByLabelText("Private key (PEM)"), new File(["K"], "k.pem"));
    await user.click(screen.getByRole("button", { name: "Install certificate" }));
    expect(await screen.findByText(/does not match the certificate/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Install certificate" })).toBeEnabled();
  });

  it("Download certificate links to the PEM URL", async () => {
    renderPage();
    const link = await screen.findByRole("link", { name: "Download certificate" });
    expect(link).toHaveAttribute("href", "/api/settings/web/certificate.pem");
    expect(link).toHaveAttribute("download");
  });

  it("an expired certificate shows the Expired callout", async () => {
    const expired = settings();
    expired.certificate = { ...expired.certificate!, expired: true };
    vi.mocked(webServerApi.get).mockResolvedValue(expired);
    renderPage();
    expect(await screen.findByText("Expired.")).toBeInTheDocument();
  });

  it("certificate_problem shows the recovery command", async () => {
    vi.mocked(webServerApi.get).mockResolvedValue(
      settings({ certificate: null, certificate_problem: "key.pem is missing from the data folder." })
    );
    renderPage();
    expect(await screen.findByText(/key.pem is missing/)).toBeInTheDocument();
    expect(screen.getByText("bacpack-admin web --self-signed")).toBeInTheDocument();
  });

  it("the page joins the unsaved-changes guard when the address is edited", async () => {
    const user = userEvent.setup();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const router = createMemoryRouter(
      [
        {
          path: "*",
          element: (
            <UnsavedChangesProvider>
              <Routes>
                <Route path="/settings/web" element={<WebServer />} />
                <Route path="*" element={<p>home</p>} />
              </Routes>
              <LeaveGuard />
            </UnsavedChangesProvider>
          ),
        },
      ],
      { initialEntries: ["/settings/web"] }
    );
    render(
      <QueryClientProvider client={client}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    );
    await user.type(await screen.findByLabelText("Address"), "1");
    await act(() => router.navigate("/"));
    expect(screen.getByRole("alertdialog", { name: "Discard unsaved changes?" })).toBeInTheDocument();
  });
});
