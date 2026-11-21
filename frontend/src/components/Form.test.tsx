import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { CheckField, Field, Fieldset, Form, FormActions, FormGrid, Segmented, SettingsColumn } from "./Form";
import { expectNamedControls } from "../testing/controls";

describe("SettingsColumn", () => {
  it("holds its panels in the one settings column", () => {
    const { container } = render(
      <SettingsColumn>
        <Fieldset legend="One">a</Fieldset>
        <Fieldset legend="Two">b</Fieldset>
      </SettingsColumn>
    );
    const column = container.querySelector(".bp-settings");
    expect(column).not.toBeNull();
    expect(column?.querySelectorAll("fieldset")).toHaveLength(2);
  });
});

describe("Form building blocks", () => {
  it("binds the label to its control and adds the input class", () => {
    render(
      <Field label="Name">
        <input />
      </Field>
    );
    const input = screen.getByLabelText("Name");
    expect(input).toHaveClass("bp-input");
    expect(input.closest(".bp-field")).not.toHaveClass("bp-field--error");
  });

  it("keeps the control's own id and gives a select the select class", () => {
    render(
      <Field label="Mode">
        <select id="trend_mode">
          <option>Interval</option>
        </select>
      </Field>
    );
    const select = screen.getByLabelText("Mode");
    expect(select).toHaveAttribute("id", "trend_mode");
    expect(select).toHaveClass("bp-select");
  });

  it("describes the control with its help line", () => {
    render(
      <Field label="Retention (days)" help="Blank keeps samples forever.">
        <input type="number" />
      </Field>
    );
    const input = screen.getByLabelText("Retention (days)");
    expect(input).toHaveAccessibleDescription("Blank keeps samples forever.");
    expect(input).not.toHaveAttribute("aria-invalid");
  });

  it("keeps a control's own description beside its help line", () => {
    render(
      <>
        <p id="own-note">Applies at the next start.</p>
        <Field label="Port" help="1-65535.">
          <input aria-describedby="own-note" />
        </Field>
        <CheckField label="Enabled" help="Polls when on." aria-describedby="own-note" />
      </>
    );
    expect(screen.getByLabelText("Port")).toHaveAccessibleDescription("Applies at the next start. 1-65535.");
    expect(screen.getByLabelText("Enabled")).toHaveAccessibleDescription("Applies at the next start. Polls when on.");
  });

  it("replaces the help with the error, marks the control invalid and announces it", () => {
    render(
      <Field label="Scale" help="Multiplies the value." error="Scale needs a number">
        <input />
      </Field>
    );
    const input = screen.getByLabelText("Scale");
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveAccessibleDescription("Scale needs a number");
    expect(screen.queryByText("Multiplies the value.")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Scale needs a number");
    expect(input.closest(".bp-field")).toHaveClass("bp-field--error");
  });

  it("adds the input class beside a modifier, and leaves a checkbox alone", () => {
    render(
      <>
        <Field label="URL">
          <input className="bp-input--mono" />
        </Field>
        <Field label="Writable">
          <input type="checkbox" />
        </Field>
      </>
    );
    expect(screen.getByLabelText("URL")).toHaveClass("bp-input", "bp-input--mono");
    expect(screen.getByLabelText("Writable")).not.toHaveClass("bp-input");
  });

  it("wraps a checkbox in its label", () => {
    render(<CheckField label="Trended" checked={false} onChange={() => {}} />);
    const box = screen.getByRole("checkbox", { name: "Trended" });
    expect(box.closest("label")).toHaveClass("bp-check");
  });

  it("lays out a form, a fieldset, a grid and the actions with the design-system classes", () => {
    const { container } = render(
      <Form aria-label="Settings">
        <Fieldset legend="Timing">
          <FormGrid>
            <Field label="Poll interval (s)">
              <input type="number" />
            </Field>
          </FormGrid>
        </Fieldset>
        <FormActions>
          <button type="submit">Save</button>
        </FormActions>
      </Form>
    );
    expect(screen.getByRole("form", { name: "Settings" })).toHaveClass("bp-form");
    expect(screen.getByRole("group", { name: "Timing" })).toHaveClass("bp-fieldset");
    expect(container.querySelector(".bp-form-grid")).not.toBeNull();
    expect(container.querySelector(".bp-form-actions")).not.toBeNull();
    expectNamedControls(container);
  });
});

describe("expectNamedControls", () => {
  it("fails on a checkbox named 'on' and on an unlabelled input", () => {
    const unnamed = render(<input type="text" />);
    expect(() => expectNamedControls(unnamed.container)).toThrow();
    unnamed.unmount();
    const on = render(
      <label>
        <input type="checkbox" /> on
      </label>
    );
    expect(() => expectNamedControls(on.container)).toThrow();
  });
});

describe("Segmented", () => {
  it("is a named radio group whose options are named radios", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const { container } = render(
      <Segmented
        label="Entry kind"
        value="date"
        options={[
          { value: "date", label: "Date" },
          { value: "week-and-day", label: "Pattern" },
        ]}
        onChange={onChange}
      />
    );
    const group = screen.getByRole("radiogroup", { name: "Entry kind" });
    expect(group).toHaveClass("bp-segmented");
    expect(screen.getByRole("radio", { name: "Date" })).toBeChecked();
    await user.click(screen.getByRole("radio", { name: "Pattern" }));
    expect(onChange).toHaveBeenCalledWith("week-and-day");
    expectNamedControls(container);
  });
});
