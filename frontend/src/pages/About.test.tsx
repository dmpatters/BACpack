import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import About from "./About";
import { capabilitiesApi } from "../api/endpoints";
import type { Capabilities } from "../api/types";

vi.mock("../api/endpoints", () => ({
  capabilitiesApi: { get: vi.fn() },
  diagnosticsApi: { downloadUrl: (real: boolean) => `/api/diagnostics/download?real_values=${real}` },
}));

const capabilities: Capabilities = {
  bacpack_version: "0.17.0",
  protocol_revision: 22,
  segmentation: "segmented-both",
  max_apdu_length: 1024,
  data_links: ["BACnet/IP", "BACnet/IP BBMD", "BACnet/IP foreign device"],
  bibb_profile_source: "B-BC, Protocol_Revision 24",
  bibb_summary: { met: 12, total: 21 },
  services: [
    { name: "read-property", kind: "confirmed", executes: true, initiates: false },
    { name: "confirmed-event-notification", kind: "confirmed", executes: false, initiates: true },
  ],
  object_types: ["analog-input", "trend-log"],
  bibbs: [
    {
      name: "DS-RP-B",
      title: "Data Sharing - ReadProperty - B",
      area: "Data Sharing",
      supported: true,
      missing: [],
      note: null,
    },
    {
      name: "DM-TS-B",
      title: "Device Management - TimeSynchronization - B",
      area: "Device Management",
      supported: false,
      missing: ["execute time-synchronization"],
      note: "B-BC requires DM-TS-B or DM-UTC-B",
    },
  ],
};

function renderAbout() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <About />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe("About", () => {
  it("describes BACpack and shows the version and license", async () => {
    vi.mocked(capabilitiesApi.get).mockResolvedValue(capabilities);
    renderAbout();

    expect(await screen.findByText(/No redundancy or high availability/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("about-facts")).toHaveTextContent("0.17.0"));
    expect(screen.getByTestId("about-facts")).toHaveTextContent("MIT");
    expect(screen.getByRole("link", { name: /github\.com\/dmpatters\/BACpack/ })).toHaveAttribute(
      "href",
      "https://github.com/dmpatters/BACpack"
    );
  });

  it("shows the BIBB summary and each BIBB's status and reason", async () => {
    vi.mocked(capabilitiesApi.get).mockResolvedValue(capabilities);
    renderAbout();

    expect(await screen.findByText("12 of 21 B-BC requirements met")).toBeInTheDocument();
    const tsRow = screen.getByRole("row", { name: /DM-TS-B/ });
    expect(within(tsRow).getByText("Not supported")).toBeInTheDocument();
    expect(tsRow).toHaveTextContent("execute time-synchronization");
    expect(screen.getByRole("columnheader", { name: "Missing" })).toBeInTheDocument();
    const rpRow = screen.getByRole("row", { name: /DS-RP-B/ });
    expect(within(rpRow).getByText("Supported")).toBeInTheDocument();
  });

  it("explains the protocol revision once, next to the requirements", async () => {
    vi.mocked(capabilitiesApi.get).mockResolvedValue(capabilities);
    renderAbout();
    const note = await screen.findByTestId("bibb-profile-source");
    expect(note).toHaveTextContent("BACpack devices advertise protocol revision 22.");
    expect(note).toHaveTextContent("B-BC, Protocol_Revision 24 of the standard");
  });

  it("shows the BIBB table with status pills and readable service names", async () => {
    vi.mocked(capabilitiesApi.get).mockResolvedValue(capabilities);
    renderAbout();
    const bibbs = await screen.findByRole("table", { name: "B-BC requirements" });
    expect(within(bibbs).getByText("Supported")).toHaveClass("bp-pill", "bp-pill--ok");
    expect(within(bibbs).getByText("Not supported")).toHaveClass("bp-pill", "bp-pill--warn");
    expect(within(bibbs).getByText("execute time-synchronization")).toBeInTheDocument();

    const services = screen.getByRole("table", { name: "Services" });
    expect(within(services).getByText("Read property")).toBeInTheDocument();
    expect(within(services).getByText("read-property")).toHaveClass("bp-mono");
    expect(within(services).getByText("Executes")).toBeInTheDocument();
  });

  it("lays out the facts as a key/value list in plain words", async () => {
    vi.mocked(capabilitiesApi.get).mockResolvedValue(capabilities);
    renderAbout();
    const facts = await screen.findByTestId("protocol-facts");
    expect(facts).toHaveClass("bp-facts");
    expect(within(facts).getByText("Both directions")).toBeInTheDocument();
    expect(within(facts).getByText("1024 bytes")).toBeInTheDocument();
    expect(screen.getByTestId("about-facts")).toHaveClass("bp-facts");
  });

  it("lists object types in a table: the name in words and the identifier", async () => {
    vi.mocked(capabilitiesApi.get).mockResolvedValue(capabilities);
    renderAbout();
    const types = await screen.findByRole("table", { name: "Object types" });
    expect(within(types).getByRole("columnheader", { name: "Object type" })).toBeInTheDocument();
    expect(within(types).getByRole("columnheader", { name: "Identifier" })).toBeInTheDocument();
    const row = within(types).getByRole("row", { name: /Analog input/ });
    expect(within(row).getByText("Analog input")).toBeInTheDocument();
    expect(within(row).getByText("analog-input")).toHaveClass("bp-mono");
    expect(within(types).getAllByRole("row")).toHaveLength(3);
  });

  it("points to the Security page for the login, not off by default", async () => {
    vi.mocked(capabilitiesApi.get).mockResolvedValue(capabilities);
    renderAbout();
    const text = await screen.findByText(/Set the web UI login/);
    expect(text).toHaveTextContent(/Security page/);
    expect(screen.queryByText(/optional and off by default/)).not.toBeInTheDocument();
  });

  it("links the conformance statement BACpack serves itself", async () => {
    vi.mocked(capabilitiesApi.get).mockResolvedValue(capabilities);
    renderAbout();
    const link = await screen.findByRole("link", { name: /PICS/ });
    expect(link).toHaveAttribute("href", "/api/pics");
    expect(link).toHaveAttribute("target", "_blank");
  });

  it("writes no double hyphen anywhere", async () => {
    vi.mocked(capabilitiesApi.get).mockResolvedValue(capabilities);
    renderAbout();
    await screen.findByTestId("protocol-facts");
    expect(document.body).not.toHaveTextContent(/ -- /);
  });

  it("shows each note once under the table, not in the rows", async () => {
    vi.mocked(capabilitiesApi.get).mockResolvedValue({
      ...capabilities,
      bibbs: [
        ...capabilities.bibbs,
        {
          name: "DM-UTC-B",
          title: "Device Management - UTCTimeSynchronization - B",
          area: "Device Management",
          supported: true,
          missing: [],
          note: "B-BC requires DM-TS-B or DM-UTC-B",
        },
      ],
    });
    renderAbout();

    const notes = await screen.findByTestId("bibb-notes");
    expect(notes).toHaveTextContent("DM-TS-B, DM-UTC-B: B-BC requires DM-TS-B or DM-UTC-B");
    expect(screen.getByRole("row", { name: /DM-UTC-B/ })).not.toHaveTextContent("requires");
  });

  it("drops the Missing column when every BIBB is met", async () => {
    vi.mocked(capabilitiesApi.get).mockResolvedValue({
      ...capabilities,
      bibb_summary: { met: 21, total: 21 },
      bibbs: capabilities.bibbs.map((b) => ({ ...b, supported: true, missing: [] })),
    });
    renderAbout();

    expect(await screen.findByText("21 of 21 B-BC requirements met")).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: /missing/i })).not.toBeInTheDocument();
    expect(screen.getByRole("row", { name: /DM-TS-B/ }).querySelectorAll("td")).toHaveLength(3);
  });

  it("lists services in both directions and the object types", async () => {
    vi.mocked(capabilitiesApi.get).mockResolvedValue(capabilities);
    renderAbout();

    const notifRow = await screen.findByRole("row", { name: /confirmed-event-notification/ });
    expect(notifRow).toHaveTextContent("Initiates");
    expect(screen.getByRole("table", { name: "Object types" })).toHaveTextContent("trend-log");
  });

  it("offers the diagnostics download with stand-ins by default", async () => {
    vi.mocked(capabilitiesApi.get).mockResolvedValue(capabilities);
    renderAbout();

    const link = await screen.findByRole("link", { name: "Download diagnostics" });
    expect(link).toHaveAttribute("href", "/api/diagnostics/download?real_values=false");
    expect(link).toHaveAttribute("download");
    const box = screen.getByRole("checkbox", { name: "Use real values" });
    expect(box).not.toBeChecked();
    expect(box).toHaveAccessibleDescription(
      "Names, addresses and URLs are replaced with stand-ins."
    );
  });

  it("shows the section before the capabilities load", () => {
    vi.mocked(capabilitiesApi.get).mockReturnValue(new Promise(() => {}));
    renderAbout();

    expect(screen.getByRole("heading", { name: "Diagnostics" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download diagnostics" })).toBeInTheDocument();
  });

  it("ticking Use real values changes the link and the help, unticking restores them", async () => {
    const user = userEvent.setup();
    vi.mocked(capabilitiesApi.get).mockResolvedValue(capabilities);
    renderAbout();

    const box = await screen.findByRole("checkbox", { name: "Use real values" });
    const link = screen.getByRole("link", { name: "Download diagnostics" });
    await user.click(box);
    expect(link).toHaveAttribute("href", "/api/diagnostics/download?real_values=true");
    expect(box).toHaveAccessibleDescription(
      "Real names, addresses and URLs. Passwords, tokens and header values stay out."
    );

    await user.click(box);
    expect(link).toHaveAttribute("href", "/api/diagnostics/download?real_values=false");
    expect(box).toHaveAccessibleDescription(/replaced with stand-ins/);
  });
});
