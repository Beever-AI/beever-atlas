/**
 * RES-287/4b — regression tests for the cancellation-guard pattern in
 * wiki/MermaidBlock.tsx. The previous implementation had no `useEffect`
 * cleanup, so React StrictMode's double-invoke fired two concurrent
 * `mermaid.render()` calls against the singleton mermaid instance, the
 * second of which produced an error SVG and slipped into the fallback
 * `<details>` tile — stacking once per block on the page.
 *
 * Pattern mirrors `web/src/components/channel/__tests__/MermaidBlock.test.tsx`.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { MermaidBlock } from "../MermaidBlock";

vi.mock("mermaid", () => ({
  default: {
    initialize: vi.fn(),
    parse: vi.fn().mockResolvedValue(true),
    render: vi.fn(),
  },
}));

// useTheme is loaded from a hook that reads CSS-class state; stub to a stable value.
vi.mock("@/hooks/useTheme", () => ({
  useTheme: () => ({ resolvedTheme: "light", setTheme: vi.fn() }),
}));

describe("wiki/MermaidBlock (RES-287/4b)", () => {
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

    render(<MermaidBlock chart="graph TD; A-->B" />);

    await waitFor(() => {
      expect(document.querySelector("svg")).not.toBeNull();
    });
  });

  it("renders exactly one fallback <details> when the diagram fails", async () => {
    const { default: mermaid } = await import("mermaid");
    // Both parse attempts (initial + simplified retry) must fail so the
    // component exhausts retries and shows the fallback.
    vi.mocked(mermaid.parse)
      .mockRejectedValueOnce(new Error("Syntax error in text"))
      .mockRejectedValueOnce(new Error("Syntax error in text"));

    render(<MermaidBlock chart="not valid mermaid" />);

    await waitFor(() => {
      expect(screen.getByText(/Diagram could not be rendered/i)).toBeInTheDocument();
    });

    // CRITICAL: exactly one fallback tile, not stacked.
    expect(screen.queryAllByText(/Diagram could not be rendered/i)).toHaveLength(1);
  });

  it("two MermaidBlocks each produce at most one fallback (no stacking)", async () => {
    const { default: mermaid } = await import("mermaid");
    vi.mocked(mermaid.parse).mockRejectedValue(new Error("bad"));

    render(
      <>
        <MermaidBlock chart="bad1" />
        <MermaidBlock chart="bad2" />
      </>,
    );

    // Wait for BOTH blocks to settle into their fallback. Each block's
    // parse→retry→fallback cycle is async and independent, so under load the
    // second can lag the first — asserting immediately after a `>= 1` wait is
    // racy (observed CI flake: got 1, expected 2). Waiting for exactly 2 also
    // encodes the no-stacking invariant: two blocks → exactly two fallback
    // summaries, never four (the symptom of the StrictMode race producing
    // duplicate setError writes from the aborted first render of each block).
    // The dedicated StrictMode test below remains the canonical guard against
    // a single block emitting a stacked second tile.
    await waitFor(() => {
      expect(screen.queryAllByText(/Diagram could not be rendered/i)).toHaveLength(2);
    });
  });

  it("StrictMode double-mount does not produce stacked error tiles", async () => {
    const { default: mermaid } = await import("mermaid");
    vi.mocked(mermaid.parse).mockRejectedValue(new Error("bad"));

    render(
      <StrictMode>
        <MermaidBlock chart="invalid in strict mode" />
      </StrictMode>,
    );

    await waitFor(() => {
      expect(screen.getByText(/Diagram could not be rendered/i)).toBeInTheDocument();
    });

    // The cancellation guard is what makes this pass. Without it, both
    // mount cycles would race, set error twice, and produce stacked tiles.
    expect(screen.queryAllByText(/Diagram could not be rendered/i)).toHaveLength(1);
  });

  it("falls back when render returns a syntax-error SVG (mermaid v11 swallow)", async () => {
    const { default: mermaid } = await import("mermaid");
    // parse() passes the syntax check, but render() returns the diagnostic
    // SVG mermaid v11 emits instead of throwing. The component must detect
    // the SVG content and treat it as an error.
    // `true` matches mermaid's runtime contract; the type cast keeps Vitest
    // happy with mermaid v11's typed ParseResult union.
    vi.mocked(mermaid.parse).mockResolvedValue(true as unknown as never);
    vi.mocked(mermaid.render)
      .mockResolvedValueOnce({
        svg: '<svg><text>Syntax error in text</text><text>mermaid version 11.15.0</text></svg>',
        bindFunctions: undefined,
        diagramType: "flowchart",
      })
      .mockResolvedValueOnce({
        svg: '<svg><text>Syntax error in text</text></svg>',
        bindFunctions: undefined,
        diagramType: "flowchart",
      });

    render(<MermaidBlock chart="graph TD; A---" />);

    await waitFor(() => {
      expect(screen.getByText(/Diagram could not be rendered/i)).toBeInTheDocument();
    });
  });
});
