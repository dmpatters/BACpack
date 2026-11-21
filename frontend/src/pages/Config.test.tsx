import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Config, { applySummary, planHeadline } from "./Config";
import { expectNamedControls } from "../testing/controls";
import { inSettingsColumn } from "../testing/columns";
import { PlanChangedError, configApi } from "../api/endpoints";
import type { ConfigSyncPlan } from "../api/types";

vi.mock("../api/endpoints", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/endpoints")>();
  return {
    PlanChangedError: actual.PlanChangedError,
    configApi: { exportUrl: () => "/api/config/export", import: vi.fn() },
  };
});

const NO_CONNECTORS = {
  connectors_created: [],
  connectors_updated: [],
  connector_mappings: {},
  needs_credentials: {},
  settings_changed: false,
};

function plan(digest: string, deleted: string[], extra: Partial<ConfigSyncPlan> = {}): ConfigSyncPlan {
  return {
    dry_run: true,
    devices_created: [],
    devices_updated: [599001],
    devices_deleted: [],
    objects: { "599001": { created: [], updated: [], deleted } },
    ...NO_CONNECTORS,
    plan_digest: digest,
    ...extra,
  };
}

function renderConfig() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <Config />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe("Config sample building", () => {
  it("offers Load sample building, which opens the Devices page's panel", () => {
    renderConfig();
    const fieldset = screen.getByRole("group", { name: "Sample building" });
    expect(within(fieldset).getByRole("link", { name: "Load sample building" })).toHaveAttribute(
      "href",
      "/devices/sample"
    );
  });
});

async function previewFile(user: ReturnType<typeof userEvent.setup>) {
  const file = new File(['{"devices": []}'], "config.json", { type: "application/json" });
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  await user.upload(input, file);
  // Submitted directly: jsdom's constraint check on the `required` file
  // input blocks a click on the submit button.
  fireEvent.submit(input.form as HTMLFormElement);
  return file;
}

async function applyInDialog(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Apply" }));
  const dialog = screen.getByRole("alertdialog", { name: "Apply this configuration?" });
  await user.click(within(dialog).getByRole("button", { name: "Apply configuration" }));
}

describe("Config import", () => {
  beforeEach(() => {
    vi.mocked(configApi.import).mockReset();
  });

  it("names the deletions in the dialog, and applies nothing on Cancel", async () => {
    const user = userEvent.setup();
    vi.mocked(configApi.import).mockResolvedValueOnce(plan("abc", ["analog-value:9"]));
    renderConfig();
    await previewFile(user);

    await user.click(await screen.findByRole("button", { name: "Apply" }));
    const dialog = screen.getByRole("alertdialog", { name: "Apply this configuration?" });
    expect(dialog).toHaveTextContent("It deletes 1 object and its trend history.");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(configApi.import).toHaveBeenCalledTimes(1);
  });

  it("sends the preview's plan digest on Apply", async () => {
    const user = userEvent.setup();
    vi.mocked(configApi.import)
      .mockResolvedValueOnce(plan("abc", []))
      .mockResolvedValueOnce({ ...plan("abc", []), dry_run: false, plan_digest: undefined });
    renderConfig();
    const file = await previewFile(user);

    await applyInDialog(user);
    await screen.findByText(/Applied:/);
    expect(configApi.import).toHaveBeenNthCalledWith(1, file, true);
    expect(configApi.import).toHaveBeenNthCalledWith(2, file, false, "abc");
  });

  it("re-runs the preview when Apply finds the plan changed", async () => {
    const user = userEvent.setup();
    vi.mocked(configApi.import)
      .mockResolvedValueOnce(plan("abc", []))
      .mockRejectedValueOnce(new PlanChangedError("the configuration changed since the preview"))
      .mockResolvedValueOnce(plan("def", ["analog-value:9"]));
    renderConfig();
    const file = await previewFile(user);

    await applyInDialog(user);
    await screen.findByText(/Nothing was applied/);
    expect(configApi.import).toHaveBeenNthCalledWith(3, file, true);
    expect(await screen.findByText(/Delete 1:/)).toHaveTextContent("Delete 1: analog-value:9");

    // The next Apply carries the new preview's digest.
    vi.mocked(configApi.import).mockResolvedValueOnce({ ...plan("def", ["analog-value:9"]), dry_run: false });
    await applyInDialog(user);
    await waitFor(() => expect(configApi.import).toHaveBeenNthCalledWith(4, file, false, "def"));
  });

  it("does not re-preview on other Apply errors", async () => {
    const user = userEvent.setup();
    vi.mocked(configApi.import)
      .mockResolvedValueOnce(plan("abc", []))
      .mockRejectedValueOnce(new Error("bind_address: bad"));
    renderConfig();
    await previewFile(user);

    await applyInDialog(user);
    await screen.findByText(/bind_address: bad/);
    expect(configApi.import).toHaveBeenCalledTimes(2);
  });

  it("shows the plan per device, with the warning just above Apply", async () => {
    const user = userEvent.setup();
    vi.mocked(configApi.import).mockResolvedValueOnce(plan("abc", ["analog-value:9"]));
    renderConfig();
    await previewFile(user);

    const planBox = await screen.findByRole("group", { name: "Plan" });
    expect(within(planBox).getByText("Updates 1 device. Deletes 1 object.")).toBeInTheDocument();
    expect(within(planBox).getByRole("heading", { name: "Device 599001" })).toBeInTheDocument();
    expect(within(planBox).getByText(/Delete 1:/)).toHaveTextContent("Delete 1: analog-value:9");
    const warning = within(planBox).getByText("Import is a full sync.");
    const applyButton = within(planBox).getByRole("button", { name: "Apply" });
    expect(warning.compareDocumentPosition(applyButton) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("says what an apply did, every count named", async () => {
    const user = userEvent.setup();
    vi.mocked(configApi.import)
      .mockResolvedValueOnce(plan("abc", []))
      .mockResolvedValueOnce({
        dry_run: false,
        devices_created: [599009],
        devices_updated: [],
        devices_deleted: [599002, 599003],
        objects: {},
        ...NO_CONNECTORS,
      });
    renderConfig();
    await previewFile(user);
    await applyInDialog(user);
    expect(await screen.findByText("Applied: created 1 device, updated 0 devices and deleted 2 devices.")).toBeInTheDocument();
  });

  it("drops the last apply's result once another file is chosen", async () => {
    const user = userEvent.setup();
    vi.mocked(configApi.import)
      .mockResolvedValueOnce(plan("abc", []))
      .mockResolvedValueOnce({
        dry_run: false,
        devices_created: [],
        devices_updated: [599001],
        devices_deleted: [],
        objects: {},
        ...NO_CONNECTORS,
      });
    renderConfig();
    await previewFile(user);
    await applyInDialog(user);
    await screen.findByText(/Applied:/);
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    await user.upload(input, new File(["{}"], "other.json", { type: "application/json" }));
    expect(screen.queryByText(/Applied:/)).not.toBeInTheDocument();
  });

  const withConnectors = {
    connectors_created: ["Weather"],
    connectors_updated: ["Room"],
    connector_mappings: {
      Weather: { created: ["599001 analog-value:1"], updated: [], deleted: [] },
      Room: { created: [], updated: ["599001 analog-value:2"], deleted: ["write analog-value,2 present-value"] },
    },
  };

  it("counts the connectors in the headline, and applySummary still says nothing is deleted for them", () => {
    const connectorsOnly = plan("abc", [], withConnectors);
    expect(planHeadline(connectorsOnly)).toContain("Creates 1 connector");
    expect(planHeadline(connectorsOnly)).toContain("updates 1 connector");
    expect(applySummary(connectorsOnly)).toBe("It deletes nothing.");
  });

  it("shows a Connectors group and each connector's mapping changes", async () => {
    const user = userEvent.setup();
    vi.mocked(configApi.import).mockResolvedValueOnce(plan("abc", [], withConnectors));
    renderConfig();
    await previewFile(user);

    const planBox = await screen.findByRole("group", { name: "Plan" });
    const group = within(planBox).getByRole("heading", { name: "Connectors" }).closest("section")!;
    expect(within(group).getByText(/Create 1:/)).toHaveTextContent("Create 1: Weather");
    expect(within(group).getByText(/Update 1:/)).toHaveTextContent("Update 1: Room");
    expect(within(group).queryByText(/Delete/)).not.toBeInTheDocument();
    const room = within(planBox).getByRole("heading", { name: "Connector Room" }).closest("section")!;
    expect(within(room).getByText(/Delete 1:/)).toHaveTextContent("Delete 1: write analog-value,2 present-value");
    expect(within(planBox).getByRole("heading", { name: "Connector Weather" })).toBeInTheDocument();
  });

  it("lists the connectors that need credentials, and only then", async () => {
    const user = userEvent.setup();
    vi.mocked(configApi.import).mockResolvedValueOnce(
      plan("abc", [], { needs_credentials: { Weather: ["password", "header X-Api-Key"] } })
    );
    renderConfig();
    await previewFile(user);
    expect(await screen.findByText("Weather: password, header X-Api-Key")).toBeInTheDocument();
    expect(screen.getByText("Needs credentials.")).toBeInTheDocument();
  });

  it("has no credentials callout or retention line when there is nothing to say", async () => {
    const user = userEvent.setup();
    vi.mocked(configApi.import).mockResolvedValueOnce(plan("abc", []));
    renderConfig();
    await previewFile(user);
    await screen.findByRole("group", { name: "Plan" });
    expect(screen.queryByText("Needs credentials.")).not.toBeInTheDocument();
    expect(screen.queryByText(/Updates retention settings/)).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Connectors" })).not.toBeInTheDocument();
  });

  it("says when the retention settings change", async () => {
    const user = userEvent.setup();
    vi.mocked(configApi.import).mockResolvedValueOnce(plan("abc", [], { settings_changed: true }));
    renderConfig();
    await previewFile(user);
    expect(await screen.findByText(/Updates retention settings\./)).toBeInTheDocument();
  });

  it("says what an apply did to connectors", async () => {
    const user = userEvent.setup();
    vi.mocked(configApi.import)
      .mockResolvedValueOnce(plan("abc", []))
      .mockResolvedValueOnce(plan("abc", [], { ...withConnectors, dry_run: false }));
    renderConfig();
    await previewFile(user);
    await applyInDialog(user);
    expect(await screen.findByText(/Connectors: created 1, updated 1\./)).toBeInTheDocument();
  });

  it("says what the import does with connectors", () => {
    renderConfig();
    expect(screen.getByText(/connectors are matched by name/i)).toBeInTheDocument();
  });

  it("says what the export leaves out", () => {
    renderConfig();
    expect(screen.getByText(/^Passwords, tokens and header values are left out/)).toBeInTheDocument();
  });

  it("names the file input", () => {
    const { container } = renderConfig();
    expectNamedControls(container);
    expect(screen.getByLabelText("Configuration file")).toHaveAttribute("type", "file");
  });

  it("puts a preview error under Preview", async () => {
    const user = userEvent.setup();
    vi.mocked(configApi.import).mockRejectedValueOnce(new Error("devices[0].id: required"));
    renderConfig();
    await previewFile(user);
    const form = screen.getByRole("button", { name: "Preview" }).closest("form")!;
    expect(await within(form).findByRole("alert")).toHaveTextContent("Couldn't preview the file: devices[0].id: required");
  });

  it("puts an apply error under the plan, leading with what failed", async () => {
    const user = userEvent.setup();
    vi.mocked(configApi.import)
      .mockResolvedValueOnce(plan("abc", []))
      .mockRejectedValueOnce(new Error("bind_address: bad"));
    renderConfig();
    await previewFile(user);
    await applyInDialog(user);
    const lead = await screen.findByText("Couldn't apply the configuration:");
    expect(lead.closest("[role=alert]")).toHaveTextContent("Couldn't apply the configuration: bind_address: bad");
    const planBox = screen.getByRole("group", { name: "Plan" });
    expect(planBox.compareDocumentPosition(lead) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(planBox.contains(lead)).toBe(false);
  });

  it("stacks Export, Import, Sample building and the plan in one settings column, saying what each does once", async () => {
    const user = userEvent.setup();
    vi.mocked(configApi.import).mockResolvedValueOnce(plan("abc", []));
    renderConfig();

    const panels = ["Export", "Import", "Sample building"].map((name) => screen.getByRole("group", { name }));
    panels.forEach((panel) => expect(inSettingsColumn(panel)).toBe(true));
    await previewFile(user);
    const planBox = await screen.findByRole("group", { name: "Plan" });
    expect(inSettingsColumn(planBox)).toBe(true);
    expect(screen.getByRole("heading", { name: "Config" }).closest(".bp-settings")).toBeNull();
    expect(screen.getAllByText(/Connectors are matched by name\./)).toHaveLength(1);
    expect(screen.getAllByText(/Passwords, tokens and header values/)).toHaveLength(1);
  });
});

describe("Config preview in flight", () => {
  // A response queued by an earlier test and never consumed would answer this one's preview.
  beforeEach(() => vi.mocked(configApi.import).mockReset());

  it("drops a preview that lands after another file was chosen", async () => {
    const user = userEvent.setup();
    let land: (value: ConfigSyncPlan) => void = () => undefined;
    vi.mocked(configApi.import).mockImplementationOnce(() => new Promise((resolve) => (land = resolve)));
    renderConfig();

    await previewFile(user);
    await waitFor(() => expect(configApi.import).toHaveBeenCalledTimes(1));
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    await user.upload(input, new File(["{}"], "other.json", { type: "application/json" }));

    await act(async () => land(plan("old", [])));
    expect(screen.queryByRole("group", { name: "Plan" })).not.toBeInTheDocument();
  });
});
