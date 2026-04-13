import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MermaidBlock } from "../channel/MermaidBlock";

// Mock mermaid so we can return a crafted "valid-SVG-containing-Syntax-error"
// response — the exact failure mode from WS-wiki-M5 that the content-sniff
// guard in MermaidBlock is supposed to catch.
vi.mock("mermaid", () => ({
  default: {
    initialize: vi.fn(),
    parse: vi.fn().mockResolvedValue(true),
    render: vi.fn(),
  },
}));

describe("MermaidBlock silent-error regression", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("detects the v11 silent-error SVG and shows the fallback", async () => {
    const { default: mermaid } = await import("mermaid");

    // Historically rendered *without* throwing — the SVG *looks* valid but
    // has "Syntax error in text" baked in.
    const silentErrorSvg =
      '<svg xmlns="http://www.w3.org/2000/svg" role="graphics-document document" ' +
      'viewBox="0 0 100 40" width="100" height="40">' +
      '<text x="10" y="20">Syntax error in text</text>' +
      '<text x="10" y="35">mermaid version 11.14.0</text>' +
      "</svg>";

    vi.mocked(mermaid.render).mockResolvedValue({
      svg: silentErrorSvg,
      bindFunctions: undefined,
      diagramType: "flowchart",
    });

    render(<MermaidBlock code={"graph TD; A -- invalid ---"} />);

    await waitFor(() => {
      expect(screen.getByText("Could not render diagram.")).toBeInTheDocument();
    });

    // The <pre> fallback should contain the original source text.
    const pre = document.querySelector("pre");
    expect(pre).not.toBeNull();
    expect(pre?.textContent?.trim()).toBe("graph TD; A -- invalid ---");

    // Confirm we did not leak the error-svg into the DOM as a rendered diagram.
    expect(document.querySelector("svg")).toBeNull();
  });
});
