import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import DeviceImportExportTab from "./DeviceImportExportTab";
import { PlanChangedError, objectsApi } from "../api/endpoints";
import type { CsvImportPlan, EdeImportInfo } from "../api/types";
import { expectNamedControls } from "../testing/controls";
import { columnOf, inSettingsColumn } from "../testing/columns";

vi.mock("../api/endpoints", () => ({
  objectsApi: { previewCsv: vi.fn(), importCsv: vi.fn(), exportCsvUrl: () => "/x.csv" },
  versionApi: { get: vi.fn().mockResolvedValue({ version: "0.39.0", service: false }) },
  PlanChangedError: class extends Error {},
}));

const edeInfo = (device: number | null, devices = [{ instance: 1000, name: "Boiler", object_count: 4 }]): EdeImportInfo => ({
  read_as: "EDE",
  project_name: "Plant",
  version_of_layout: "2.3",
  file_notes: [],
  devices,
  device,
});

const twoInFile = [
  { instance: 1000, name: "Boiler", object_count: 4 },
  { instance: 2000, name: "Chiller", object_count: 1 },
];

const edePlan = (overrides: Partial<CsvImportPlan> = {}): CsvImportPlan => ({
  dry_run: true,
  mode: "merge",
  created: ["analog-value:1"],
  updated: [],
  deleted: [],
  skipped_columns: [],
  plan_digest: "e1",
  skipped: [{ object_type: "event-enrollment", instance: 1, name: "Loop", reason: "not supported", code: "unsupported-type" }],
  notes: [{ object_type: "analog-input", instance: 2, name: "Temp", note: "units '---' dropped" }],
  skipped_types: { "event-enrollment (9)": 1 },
  ede: edeInfo(1000),
  ...overrides,
});

function renderTab() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <DeviceImportExportTab deviceId={599001} />
    </QueryClientProvider>
  );
}

// Submitted directly: jsdom's constraint check on the `required` file input
// blocks a click on the submit button.
async function preview(user: ReturnType<typeof userEvent.setup>, mode: "merge" | "sync" = "merge") {
  if (mode === "sync") await user.selectOptions(screen.getByLabelText("Import mode"), "sync");
  const input = screen.getByLabelText("CSV file") as HTMLInputElement;
  await user.upload(input, new File(["x"], "points.csv", { type: "text/csv" }));
  fireEvent.submit(input.form as HTMLFormElement);
}

describe("DeviceImportExportTab", () => {
  beforeEach(() => {
    vi.mocked(objectsApi.previewCsv).mockReset();
    vi.mocked(objectsApi.importCsv).mockReset();
  });

  it("stacks Export, Import and the plan in one settings column", async () => {
    vi.mocked(objectsApi.previewCsv).mockResolvedValue({
      dry_run: true, mode: "merge", created: ["analog-value:1"], updated: [], deleted: [], skipped_columns: [], plan_digest: "abc",
    });
    const user = userEvent.setup();
    renderTab();

    for (const name of ["Export", "Import"]) {
      const panel = screen.getByRole("group", { name });
      expect(inSettingsColumn(panel)).toBe(true);
      expect(columnOf(panel)).toBeNull();
    }
    await preview(user);
    expect(inSettingsColumn(await screen.findByRole("group", { name: "Plan" }))).toBe(true);
  });

  it("shows the import result inside the Import panel", async () => {
    vi.mocked(objectsApi.previewCsv).mockResolvedValue({
      dry_run: true, mode: "merge", created: ["analog-value:1"], updated: [], deleted: [], skipped_columns: [], plan_digest: "abc",
    });
    vi.mocked(objectsApi.importCsv).mockResolvedValue({ imported: 1 });
    const user = userEvent.setup();
    renderTab();
    await preview(user);
    await user.click(await screen.findByRole("button", { name: "Import" }));

    const done = await screen.findByText("Imported 1 object.");
    expect(screen.getByRole("group", { name: "Import" })).toContainElement(done);
  });

  it("previews a merge, then imports with the preview's digest", async () => {
    vi.mocked(objectsApi.previewCsv).mockResolvedValue({
      dry_run: true, mode: "merge", created: ["analog-value:1"], updated: [], deleted: [], skipped_columns: [], plan_digest: "abc",
    });
    vi.mocked(objectsApi.importCsv).mockResolvedValue({ imported: 1 });
    const user = userEvent.setup();
    renderTab();
    await preview(user);

    expect(await screen.findByText(/Create 1:/)).toBeInTheDocument();
    expect(objectsApi.previewCsv).toHaveBeenCalledWith(599001, expect.any(File), "merge");
    await user.click(screen.getByRole("button", { name: "Import" }));
    expect(objectsApi.importCsv).toHaveBeenCalledWith(599001, expect.any(File), "merge", "abc");
    expect(await screen.findByText("Imported 1 object.")).toBeInTheDocument();
    expect(screen.queryByText(/Create 1:/)).not.toBeInTheDocument();
  });

  it("warns before a sync and asks before deleting", async () => {
    vi.mocked(objectsApi.previewCsv).mockResolvedValue({
      dry_run: true, mode: "sync", created: [], updated: ["analog-value:1"], deleted: ["analog-value:2"], skipped_columns: [], plan_digest: "d",
    });
    vi.mocked(objectsApi.importCsv).mockResolvedValue({ created: [], updated: ["analog-value:1"], deleted: ["analog-value:2"] });
    const user = userEvent.setup();
    renderTab();
    await preview(user, "sync");

    const warning = await screen.findByText(/Sync replaces this device's object list/);
    const importButton = screen.getByRole("button", { name: "Import" });
    expect(warning.compareDocumentPosition(importButton) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await user.click(importButton);
    const asked = await screen.findByRole("alertdialog", { name: "Import points.csv?" });
    await user.click(within(asked).getByRole("button", { name: "Cancel" }));
    expect(objectsApi.importCsv).not.toHaveBeenCalled();
    await user.click(importButton);
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("deletes 1 object and its trend history");
    await user.click(within(dialog).getByRole("button", { name: "Import and delete" }));
    expect(await screen.findByText("Updated 1 object and deleted 1 object.")).toBeInTheDocument();
  });

  it("imports a sync that deletes nothing without asking", async () => {
    vi.mocked(objectsApi.previewCsv).mockResolvedValue({
      dry_run: true, mode: "sync", created: ["analog-value:3"], updated: [], deleted: [], skipped_columns: [], plan_digest: "d",
    });
    vi.mocked(objectsApi.importCsv).mockResolvedValue({ created: ["analog-value:3"], updated: [], deleted: [] });
    const user = userEvent.setup();
    renderTab();
    await preview(user, "sync");

    expect(await screen.findByText("Importing deletes no objects.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Import" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(await screen.findByText("Created 1 object.")).toBeInTheDocument();
  });

  it("re-previews when the device changed since the preview", async () => {
    vi.mocked(objectsApi.previewCsv)
      .mockResolvedValueOnce({ dry_run: true, mode: "merge", created: ["analog-value:1"], updated: [], deleted: [], skipped_columns: [], plan_digest: "old" })
      .mockResolvedValueOnce({
        dry_run: true, mode: "merge", created: ["analog-value:1", "analog-value:2"], updated: [], deleted: [], skipped_columns: [], plan_digest: "new",
      });
    vi.mocked(objectsApi.importCsv).mockRejectedValueOnce(new PlanChangedError("changed"));
    const user = userEvent.setup();
    renderTab();
    await preview(user);
    await user.click(await screen.findByRole("button", { name: "Import" }));

    expect(await screen.findByText(/Nothing was imported:/)).toBeInTheDocument();
    expect(await screen.findByText(/Create 2:/)).toBeInTheDocument();
    vi.mocked(objectsApi.importCsv).mockResolvedValueOnce({ imported: 2 });
    await user.click(screen.getByRole("button", { name: "Import" }));
    expect(objectsApi.importCsv).toHaveBeenLastCalledWith(599001, expect.any(File), "merge", "new");
  });

  it("shows a refused import under the plan", async () => {
    vi.mocked(objectsApi.previewCsv).mockResolvedValue({
      dry_run: true, mode: "merge", created: ["analog-value:1"], updated: [], deleted: [], skipped_columns: [], plan_digest: "abc",
    });
    vi.mocked(objectsApi.importCsv).mockRejectedValueOnce(new Error("analog-value:1 already exists"));
    const user = userEvent.setup();
    renderTab();
    await preview(user);
    await user.click(await screen.findByRole("button", { name: "Import" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't import: analog-value:1 already exists");
  });

  it("names the columns the import leaves out, and only when there are some", async () => {
    vi.mocked(objectsApi.previewCsv)
      .mockResolvedValueOnce({
        dry_run: true, mode: "merge", created: ["analog-input:1"], updated: [], deleted: [],
        skipped_columns: ["Panel", "column 4 (no header)"], plan_digest: "abc",
      })
      .mockResolvedValueOnce({
        dry_run: true, mode: "merge", created: ["analog-input:1"], updated: [], deleted: [], skipped_columns: [], plan_digest: "abc",
      });
    const user = userEvent.setup();
    renderTab();
    await preview(user);
    const plan = await screen.findByRole("group", { name: "Plan" });
    expect(plan).toHaveTextContent("Columns not imported: Panel, column 4 (no header)");

    await preview(user);
    await screen.findByText(/Create 1:/);
    expect(screen.queryByText(/Columns not imported/)).not.toBeInTheDocument();
  });

  it("drops the preview when the mode changes", async () => {
    vi.mocked(objectsApi.previewCsv).mockResolvedValue({
      dry_run: true, mode: "merge", created: ["analog-value:1"], updated: [], deleted: [], skipped_columns: [], plan_digest: "abc",
    });
    const user = userEvent.setup();
    renderTab();
    await preview(user);
    await screen.findByText(/Create 1:/);
    await user.selectOptions(screen.getByLabelText("Import mode"), "sync");
    expect(screen.queryByRole("button", { name: "Import" })).not.toBeInTheDocument();
  });

  it("names every control", () => {
    const { container } = renderTab();
    expectNamedControls(container);
  });
});

describe("DeviceImportExportTab with an EDE file", () => {
  beforeEach(() => {
    vi.mocked(objectsApi.previewCsv).mockReset();
    vi.mocked(objectsApi.importCsv).mockReset();
  });

  it("says the file was read as EDE and lists what it skipped and changed", async () => {
    vi.mocked(objectsApi.previewCsv).mockResolvedValue(edePlan());
    const user = userEvent.setup();
    renderTab();
    await preview(user);
    const plan = await screen.findByRole("group", { name: "Plan" });
    expect(plan).toHaveTextContent("Read as an EDE file (project Plant, layout version 2.3).");
    expect(within(plan).getByText(/Not imported \(1\)/)).toBeInTheDocument();
    expect(within(plan).getByText(/units '---' dropped/)).toBeInTheDocument();
    const link = await within(plan).findByRole("link", { name: "Request support for these types" });
    expect(new URL(link.getAttribute("href")!).searchParams.get("title")).toContain("EDE imports");
    expect(screen.queryByLabelText("Device in the file")).not.toBeInTheDocument();
  });

  it("a file with several devices asks which, holds Import back, then previews the one picked", async () => {
    vi.mocked(objectsApi.previewCsv)
      .mockResolvedValueOnce(
        edePlan({ created: [], skipped: [], notes: [], skipped_types: {}, plan_digest: undefined, needs_device: true, ede: edeInfo(null, twoInFile) })
      )
      .mockResolvedValueOnce(edePlan({ ede: edeInfo(2000, twoInFile), plan_digest: "e2" }));
    vi.mocked(objectsApi.importCsv).mockResolvedValue({ imported: 1 });
    const user = userEvent.setup();
    renderTab();
    await preview(user);
    const select = await screen.findByLabelText("Device in the file");
    expect(screen.queryByRole("button", { name: "Import" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Create 1:/)).not.toBeInTheDocument();

    await user.selectOptions(select, "2000");
    expect(objectsApi.previewCsv).toHaveBeenLastCalledWith(599001, expect.any(File), "merge", {
      stateTexts: null,
      edeDevice: 2000,
    });
    await user.click(await screen.findByRole("button", { name: "Import" }));
    expect(objectsApi.importCsv).toHaveBeenCalledWith(599001, expect.any(File), "merge", "e2", {
      stateTexts: null,
      edeDevice: 2000,
    });
  });

  it("sends the state-texts file with the preview and the import", async () => {
    vi.mocked(objectsApi.previewCsv).mockResolvedValue(edePlan());
    vi.mocked(objectsApi.importCsv).mockResolvedValue({ imported: 1 });
    const user = userEvent.setup();
    renderTab();
    const texts = new File(["1;Off;On\n"], "states.csv");
    await user.upload(screen.getByLabelText("State texts file"), texts);
    await preview(user);
    await screen.findByText(/Read as an EDE file/);
    expect(objectsApi.previewCsv).toHaveBeenCalledWith(599001, expect.any(File), "merge", {
      stateTexts: texts,
      edeDevice: undefined,
    });
    await user.click(screen.getByRole("button", { name: "Import" }));
    expect(objectsApi.importCsv).toHaveBeenCalledWith(599001, expect.any(File), "merge", "e1", {
      stateTexts: texts,
      edeDevice: 1000,
    });
  });

  it("warns that a sync keeping and adding nothing empties the device", async () => {
    vi.mocked(objectsApi.previewCsv).mockResolvedValue(
      edePlan({ mode: "sync", created: [], updated: [], deleted: ["analog-value:1", "analog-value:2"] })
    );
    const user = userEvent.setup();
    renderTab();
    await preview(user, "sync");
    const warning = await screen.findByText(/This empties the device/);
    expect(warning.closest(".bp-callout")).toHaveTextContent("deletes all 2 objects");
  });

  it("a plain CSV preview shows none of the EDE parts", async () => {
    vi.mocked(objectsApi.previewCsv).mockResolvedValue({
      dry_run: true, mode: "sync", created: ["analog-value:1"], updated: [], deleted: ["analog-value:2"], skipped_columns: [], plan_digest: "abc",
    });
    const user = userEvent.setup();
    renderTab();
    await preview(user, "sync");
    await screen.findByText(/Sync replaces this device's object list/);
    expect(screen.queryByText(/Read as an EDE file/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Not imported/)).not.toBeInTheDocument();
    expect(screen.queryByText(/This empties the device/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Device in the file")).not.toBeInTheDocument();
  });
});

describe("DeviceImportExportTab preview in flight", () => {
  beforeEach(() => {
    vi.mocked(objectsApi.previewCsv).mockReset();
    vi.mocked(objectsApi.importCsv).mockReset();
  });

  const merged: CsvImportPlan = {
    dry_run: true, mode: "merge", created: ["analog-value:1"], updated: [], deleted: [], skipped_columns: [], plan_digest: "old",
  };

  it("drops a preview that lands after the import mode changed", async () => {
    let land: (value: CsvImportPlan) => void = () => undefined;
    vi.mocked(objectsApi.previewCsv).mockImplementationOnce(() => new Promise((resolve) => (land = resolve)));
    const user = userEvent.setup();
    renderTab();
    await preview(user);
    await user.selectOptions(screen.getByLabelText("Import mode"), "sync");

    await act(async () => land(merged));
    expect(screen.queryByRole("group", { name: "Plan" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Import" })).not.toBeInTheDocument();
  });

  it("drops a preview that lands after another file was chosen", async () => {
    let land: (value: CsvImportPlan) => void = () => undefined;
    vi.mocked(objectsApi.previewCsv).mockImplementationOnce(() => new Promise((resolve) => (land = resolve)));
    const user = userEvent.setup();
    renderTab();
    await preview(user);
    await user.upload(screen.getByLabelText("CSV file"), new File(["y"], "other.csv", { type: "text/csv" }));

    await act(async () => land(merged));
    expect(screen.queryByRole("group", { name: "Plan" })).not.toBeInTheDocument();
  });
});
