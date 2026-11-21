import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import UrlText from "./UrlText";

describe("UrlText", () => {
  it("shows the whole URL with a break point after each separator", () => {
    const url = "https://example.com/v1/forecast?lat=1&vars=a,b";
    const { container } = render(<UrlText url={url} />);
    expect(screen.getByText(url)).toBeInTheDocument();
    // https:/ / example.com/ v1/ forecast? lat=1& vars=a, b
    expect(container.querySelectorAll("wbr")).toHaveLength(7);
  });
});
