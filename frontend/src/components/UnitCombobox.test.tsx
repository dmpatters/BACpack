import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import UnitCombobox from "./UnitCombobox";
import { unitsApi } from "../api/endpoints";

vi.mock("../api/endpoints", () => ({ unitsApi: { list: vi.fn() } }));

function renderPicker(value: string, onChange: (value: string) => void = () => {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <label htmlFor="u">Units</label>
      <UnitCombobox id="u" value={value} onChange={onChange} />
    </QueryClientProvider>
  );
}

describe("UnitCombobox", () => {
  beforeEach(() => vi.clearAllMocks());

  it("doesn't call a stored unit non-standard while the units load", () => {
    vi.mocked(unitsApi.list).mockReturnValue(new Promise(() => {}));
    renderPicker("degreesFahrenheit");
    expect(screen.getByRole("combobox", { name: "Units" })).not.toHaveValue("degreesFahrenheit (not a BACnet unit)");
  });

  it("marks a stored unit that isn't in the loaded list, and lets None clear it", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    vi.mocked(unitsApi.list).mockResolvedValue({ units: ["percent"] });
    renderPicker("degF", onChange);
    const input = await screen.findByDisplayValue("degF (not a BACnet unit)");
    await user.clear(input);
    await user.type(input, "none");
    await user.keyboard("{Enter}");
    expect(onChange).toHaveBeenCalledWith("");
  });

  it("says when the units couldn't load, and retries", async () => {
    const user = userEvent.setup();
    vi.mocked(unitsApi.list).mockRejectedValueOnce(new Error("boom")).mockResolvedValue({ units: ["percent"] });
    renderPicker("");
    expect(await screen.findByText("Couldn't load the units:")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("combobox", { name: "Units" })).toBeInTheDocument();
    expect(screen.queryByText("Couldn't load the units:")).not.toBeInTheDocument();
  });
});
