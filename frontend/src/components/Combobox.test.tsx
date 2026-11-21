import { useState } from "react";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import Combobox, { filterOptions, type ComboOption } from "./Combobox";

const OPTIONS: ComboOption[] = [
  { value: "", label: "None", group: "" },
  { value: "degreesFahrenheit", label: "Degrees Fahrenheit (°F)", group: "Common", hint: "degreesFahrenheit" },
  { value: "percent", label: "Percent (%)", group: "Common", hint: "percent" },
  { value: "degreesKelvin", label: "Degrees Kelvin", group: "Temperature", hint: "degreesKelvin" },
];

function Harness({ initial = "", onChange = vi.fn() }: { initial?: string; onChange?: (v: string) => void }) {
  const [value, setValue] = useState(initial);
  return (
    <>
      <label htmlFor="u">Units</label>
      <Combobox
        id="u"
        options={OPTIONS}
        value={value}
        onChange={(v) => {
          setValue(v);
          onChange(v);
        }}
        emptyText={(q) => `No units match "${q}"`}
      />
      <button type="button">elsewhere</button>
    </>
  );
}

describe("filterOptions", () => {
  it("matches the label or the enum, ignoring case", () => {
    expect(filterOptions(OPTIONS, "fahr").map((o) => o.value)).toEqual(["degreesFahrenheit"]);
    expect(filterOptions(OPTIONS, "DEGREESK").map((o) => o.value)).toEqual(["degreesKelvin"]);
    expect(filterOptions(OPTIONS, "  ")).toHaveLength(4);
  });
});

describe("Combobox", () => {
  it("is a labelled combobox showing the chosen label", () => {
    render(<Harness initial="percent" />);
    const input = screen.getByRole("combobox", { name: "Units" });
    expect(input).toHaveValue("Percent (%)");
    expect(input).toHaveAttribute("aria-expanded", "false");
    expect(input).toHaveAttribute("aria-autocomplete", "list");
  });

  it("opens on ArrowDown with groups, the enum as a hint, and the chosen option marked", async () => {
    const user = userEvent.setup();
    render(<Harness initial="percent" />);
    await user.click(screen.getByRole("combobox"));
    await user.keyboard("{ArrowDown}");
    const list = screen.getByRole("listbox");
    expect(screen.getByRole("combobox")).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("combobox")).toHaveAttribute("aria-controls", list.id);
    expect(within(list).getByText("Common")).toHaveClass("bp-combo__group");
    expect(within(list).getByText("Temperature")).toHaveClass("bp-combo__group");
    expect(within(list).getByRole("option", { name: /Percent/ })).toHaveAttribute("aria-selected", "true");
    expect(within(list).getAllByText("degreesKelvin")[0]).toHaveClass("bp-combo__hint");
  });

  it("names the list it controls only while that list is shown", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const input = screen.getByRole("combobox");
    expect(input).not.toHaveAttribute("aria-controls");
    await user.type(input, "zzz");
    expect(screen.getByRole("status")).toHaveTextContent('No units match "zzz"');
    expect(input).not.toHaveAttribute("aria-controls");
  });

  it("highlights a match in the enum hint too", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.type(screen.getByRole("combobox"), "degreesk");
    const option = within(screen.getByRole("listbox")).getByRole("option");
    expect(option.querySelector(".bp-combo__hint mark")).toHaveTextContent("degreesK");
  });

  it("filters as you type, highlights the match, and picks with Enter", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    await user.type(screen.getByRole("combobox"), "kel");
    const options = within(screen.getByRole("listbox")).getAllByRole("option");
    expect(options).toHaveLength(1);
    expect(options[0].querySelector("mark")).toHaveTextContent("Kel");
    expect(screen.getByRole("combobox")).toHaveAttribute("aria-activedescendant", options[0].id);
    await user.keyboard("{Enter}");
    expect(onChange).toHaveBeenCalledWith("degreesKelvin");
    expect(screen.getByRole("combobox")).toHaveValue("Degrees Kelvin");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("moves the active option with the arrows, wrapping round", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("combobox"));
    await user.keyboard("{ArrowDown}");
    const input = screen.getByRole("combobox");
    const options = within(screen.getByRole("listbox")).getAllByRole("option");
    expect(input).toHaveAttribute("aria-activedescendant", options[0].id);
    await user.keyboard("{ArrowUp}");
    expect(input).toHaveAttribute("aria-activedescendant", options[3].id);
    expect(options[3]).toHaveAttribute("data-active");
  });

  it("says when nothing matches", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.type(screen.getByRole("combobox"), "xyz");
    expect(screen.getByText('No units match "xyz"')).toHaveClass("bp-combo__empty");
  });

  it("never takes free text: Escape or leaving the field puts the chosen label back", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness initial="percent" onChange={onChange} />);
    const input = screen.getByRole("combobox");
    await user.clear(input);
    await user.type(input, "degF");
    await user.keyboard("{Escape}");
    expect(input).toHaveValue("Percent (%)");
    await user.type(input, "fahr");
    await user.click(screen.getByRole("button", { name: "elsewhere" }));
    expect(input).toHaveValue("Percent (%)");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("picks with the mouse", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    await user.click(screen.getByRole("combobox"));
    await user.keyboard("{ArrowDown}");
    await user.click(screen.getByRole("option", { name: /Kelvin/ }));
    expect(onChange).toHaveBeenCalledWith("degreesKelvin");
  });
});
