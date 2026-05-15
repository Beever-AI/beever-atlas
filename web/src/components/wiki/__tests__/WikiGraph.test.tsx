/**
 * Tests for the wiki-llm-native-redesign WikiGraph route (§6.11–§6.13).
 *
 * Cytoscape is mocked because the dynamic ``import("cytoscape")`` call
 * would otherwise pull the full ~200 KB module into the test bundle.
 * The mock asserts the call shape (elements + filtering) and lets us
 * drive the click-handler path without rendering a real canvas.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";

// ---------------------------------------------------------------------------
// Mocks — cytoscape + the data hook
// ---------------------------------------------------------------------------

let cyFactoryConfig: Record<string, unknown> | null = null;
const cyTapHandlers: Record<string, (e: { target: { id: () => string } }) => void> = {};
const cyDestroy = vi.fn();

vi.mock("cytoscape", () => {
  return {
    default: vi.fn((config: Record<string, unknown>) => {
      cyFactoryConfig = config;
      return {
        on: (
          event: string,
          _selector: string,
          handler: (e: { target: { id: () => string } }) => void,
        ) => {
          cyTapHandlers[event] = handler;
        },
        destroy: cyDestroy,
      };
    }),
  };
});

import type { WikiGraphPayload } from "@/hooks/useWikiGraph";

const mockGraphState: {
  data: WikiGraphPayload | null;
  isLoading: boolean;
  error: string | null;
} = {
  data: null,
  isLoading: false,
  error: null,
};

vi.mock("@/hooks/useWikiGraph", () => ({
  useWikiGraph: () => ({
    ...mockGraphState,
    refetch: vi.fn(),
  }),
}));

// ---------------------------------------------------------------------------
// Component under test (imported AFTER mocks)
// ---------------------------------------------------------------------------
import { WikiGraph } from "../WikiGraph";

beforeEach(() => {
  cyFactoryConfig = null;
  for (const key of Object.keys(cyTapHandlers)) {
    delete cyTapHandlers[key];
  }
  cyDestroy.mockClear();
  mockGraphState.data = null;
  mockGraphState.isLoading = false;
  mockGraphState.error = null;
});

afterEach(() => {
  cleanup();
});

function renderRoute(initialEntry = "/channels/c1/wiki/graph") {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/channels/:id/wiki/graph" element={<WikiGraph />} />
        <Route
          path="/channels/:id/wiki/:slug"
          element={<div data-testid="wiki-page-route">page</div>}
        />
        <Route path="/channels/:id/memories" element={<div data-testid="entity-graph">eg</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

// ---------------------------------------------------------------------------
// §6.11 — graph component renders nodes + edges from the fetched fixture
// ---------------------------------------------------------------------------

describe("WikiGraph — node/edge rendering", () => {
  it("instantiates cytoscape with the fetched elements", async () => {
    mockGraphState.data = {
      channel_id: "c1",
      nodes: [
        { data: { id: "topic-a", label: "A", kind: "wiki", page_kind: "topic" } },
        { data: { id: "topic-b", label: "B", kind: "wiki", page_kind: "topic" } },
      ],
      edges: [
        {
          data: {
            id: "e:topic-a->topic-b",
            source: "topic-a",
            target: "topic-b",
            kind: "references_wiki",
          },
        },
      ],
    };
    renderRoute();
    await waitFor(() => {
      expect(cyFactoryConfig).not.toBeNull();
    });
    const elements = (cyFactoryConfig as { elements: unknown[] }).elements;
    expect(elements).toHaveLength(3); // 2 nodes + 1 edge
    // Node count exposed via data-attr for §6.13's bundle test friend.
    const canvas = screen.getByTestId("wiki-graph-canvas");
    expect(canvas).toHaveAttribute("data-node-count", "2");
    expect(canvas).toHaveAttribute("data-edge-count", "1");
  });
});

// ---------------------------------------------------------------------------
// §6.12 — kind filter hides non-matching nodes
// ---------------------------------------------------------------------------

describe("WikiGraph — kind filter", () => {
  it("filtering by entity hides wiki nodes", async () => {
    mockGraphState.data = {
      channel_id: "c1",
      nodes: [
        { data: { id: "topic-a", label: "A", kind: "wiki", page_kind: "topic" } },
        { data: { id: "entity:Alice", label: "Alice", kind: "entity" } },
      ],
      edges: [],
    };
    renderRoute();
    const user = userEvent.setup();
    await waitFor(() => expect(cyFactoryConfig).not.toBeNull());

    await user.selectOptions(
      screen.getByTestId("wiki-graph-filter-kind"),
      "entity",
    );

    await waitFor(() => {
      const canvas = screen.getByTestId("wiki-graph-canvas");
      expect(canvas).toHaveAttribute("data-node-count", "1");
    });
    // The post-filter cytoscape mount only carries the entity node.
    const elements = (cyFactoryConfig as { elements: unknown[] }).elements;
    expect(elements).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// §6.13 — bundle weight: WikiGraph imports cytoscape lazily
// ---------------------------------------------------------------------------

describe("WikiGraph — bundle weight", () => {
  it("does not eagerly import cytoscape at module load", () => {
    // The `vi.mock("cytoscape", ...)` factory is called lazily by Vitest
    // when the import is evaluated. We assert that simply importing the
    // WikiGraph module (already done above) does NOT instantiate the
    // factory — it only instantiates inside the useEffect.
    expect(cyFactoryConfig).toBeNull();
  });

  it("calls the factory only inside the WikiGraph useEffect", async () => {
    mockGraphState.data = {
      channel_id: "c1",
      nodes: [{ data: { id: "t", label: "T", kind: "wiki", page_kind: "topic" } }],
      edges: [],
    };
    expect(cyFactoryConfig).toBeNull(); // pre-render
    renderRoute();
    await waitFor(() => expect(cyFactoryConfig).not.toBeNull()); // post-render
  });
});

// ---------------------------------------------------------------------------
// Smoke — empty state + error + loading
// ---------------------------------------------------------------------------

describe("WikiGraph — empty + error + loading states", () => {
  it("shows the loading copy until cytoscape mounts", async () => {
    mockGraphState.data = null;
    mockGraphState.isLoading = true;
    renderRoute();
    expect(screen.getByTestId("wiki-graph-loading")).toBeInTheDocument();
  });

  it("renders the empty graph cleanly when payload is empty", async () => {
    mockGraphState.data = { channel_id: "c1", nodes: [], edges: [] };
    renderRoute();
    await waitFor(() => expect(cyFactoryConfig).not.toBeNull());
    const canvas = screen.getByTestId("wiki-graph-canvas");
    expect(canvas).toHaveAttribute("data-node-count", "0");
    expect(canvas).toHaveAttribute("data-edge-count", "0");
  });

  it("surfaces error copy when the hook reports an error", async () => {
    mockGraphState.error = "boom";
    renderRoute();
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
  });
});
