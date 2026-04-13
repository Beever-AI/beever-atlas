import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MermaidBlock } from "../MermaidBlock";

// Mock the mermaid module
vi.mock("mermaid", () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn(),
  },
}));

describe("MermaidBlock", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders SVG for valid mermaid code", async () => {
    const { default: mermaid } = await import("mermaid");
    vi.mocked(mermaid.render).mockResolvedValue({
      svg: "<svg><text>diagram</text></svg>",
      bindFunctions: undefined,
      diagramType: "flowchart",
    });

    render(<MermaidBlock code="graph TD; A-->B" />);

    await waitFor(() => {
      expect(document.querySelector("svg")).not.toBeNull();
    });

    expect(mermaid.render).toHaveBeenCalledOnce();
  });

  it("falls back to pre for invalid code", async () => {
    const { default: mermaid } = await import("mermaid");
    vi.mocked(mermaid.render).mockRejectedValue(new Error("Parse error"));

    const code = "invalid mermaid %%% syntax";
    render(<MermaidBlock code={code} />);

    await waitFor(() => {
      expect(screen.getByText("Could not render diagram.")).toBeInTheDocument();
    });

    const pre = document.querySelector("pre");
    expect(pre).not.toBeNull();
    expect(pre?.textContent).toBe(code);
  });
});
