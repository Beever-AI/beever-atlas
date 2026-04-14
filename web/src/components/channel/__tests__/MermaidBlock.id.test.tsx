import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, waitFor } from "@testing-library/react";
import { MermaidBlock } from "../MermaidBlock";

let idCounter = 0;
vi.mock("react", async () => {
  const actual = await vi.importActual<typeof import("react")>("react");
  return {
    ...actual,
    useId: () => {
      idCounter += 1;
      // Simulate React-generated ids that start with a digit or dash —
      // both are illegal prefixes for mermaid IDs.
      return idCounter % 2 === 0 ? `:r${idCounter}:` : `-${idCounter}-foo`;
    },
  };
});

vi.mock("mermaid", () => ({
  default: {
    initialize: vi.fn(),
    parse: vi.fn().mockResolvedValue(true),
    render: vi.fn().mockResolvedValue({
      svg: "<svg><text>ok</text></svg>",
      bindFunctions: undefined,
      diagramType: "flowchart",
    }),
  },
}));

describe("MermaidBlock ID sanitization + init memoization", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    idCounter = 0;
  });

  it("prefixes IDs so they always start with a letter", async () => {
    const { default: mermaid } = await import("mermaid");
    render(<MermaidBlock code="graph TD; A-->B" />);

    await waitFor(() => {
      expect(mermaid.render).toHaveBeenCalled();
    });

    const calledId = vi.mocked(mermaid.render).mock.calls[0]![0] as string;
    expect(calledId).toMatch(/^m[a-zA-Z0-9]*$/);
    expect(calledId.startsWith("-")).toBe(false);
    expect(/^[0-9]/.test(calledId)).toBe(false);
  });

  it("does not double-init mermaid across concurrent mounts", async () => {
    const { default: mermaid } = await import("mermaid");
    const initBefore = vi.mocked(mermaid.initialize).mock.calls.length;
    const renderBefore = vi.mocked(mermaid.render).mock.calls.length;

    render(<MermaidBlock code="graph TD; A-->B" />);
    render(<MermaidBlock code="graph TD; C-->D" />);
    render(<MermaidBlock code="graph TD; E-->F" />);

    await waitFor(() => {
      expect(vi.mocked(mermaid.render).mock.calls.length - renderBefore).toBe(3);
    });

    // Singleton init promise means initialize is called at most once
    // across the lifetime of the module — additional mounts must not
    // trigger new initialize() calls.
    const initAfter = vi.mocked(mermaid.initialize).mock.calls.length;
    expect(initAfter - initBefore).toBe(0);
    expect(initAfter).toBeLessThanOrEqual(1);
  });
});
