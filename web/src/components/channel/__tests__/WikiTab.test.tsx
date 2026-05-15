/**
 * WikiTab smoke test.
 *
 * The original tests in this file guarded against a c23c955-era crash
 * where WikiTab read `channelPolicy.effective.wiki.maintenance_mode`
 * to decide whether to render the Maintain Wiki button. The action
 * redesign removed the Maintain button entirely (folded into the new
 * "Update wiki" primary action), so WikiTab no longer touches
 * useChannelPolicy. Those regression cases are now dead.
 *
 * What's left here: a single smoke test that the component mounts
 * cleanly in the empty-state path. Catches gross runtime errors
 * (broken imports, hook ordering bugs, missing required props) at the
 * cheapest possible level.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";

// ---------------------------------------------------------------------------
// Hook mocks. Each must be declared BEFORE the component import below so the
// vi.mock() factory replacements are wired into the module graph.
// ---------------------------------------------------------------------------

vi.mock("@/hooks/useWiki", () => ({
  useWiki: () => ({
    data: null,
    isLoading: false,
    error: null,
    isNotFound: true,
    refetch: vi.fn(),
  }),
}));

vi.mock("@/hooks/useChannelMemoryCount", () => ({
  useChannelMemoryCount: () => ({
    hasMemories: false,
    isLoading: false,
  }),
}));

vi.mock("@/hooks/useWikiPage", () => ({
  useWikiPage: () => ({ data: null, isLoading: false, isRevalidating: false }),
}));

vi.mock("@/hooks/useWikiVersions", () => ({
  useWikiVersions: () => ({ data: [], isLoading: false, refetch: vi.fn() }),
}));

vi.mock("@/hooks/useWikiVersion", () => ({
  useWikiVersion: () => ({ data: null, isLoading: false }),
}));

vi.mock("@/hooks/useWikiRefresh", () => ({
  useWikiRefresh: () => ({
    mutate: vi.fn(),
    isPending: false,
    error: null,
    generationStatus: null,
  }),
}));

vi.mock("@/hooks/useExtractionStatus", () => ({
  useExtractionStatus: () => ({ status: null }),
}));

vi.mock("@/lib/api", () => ({
  api: {
    get: vi.fn(async () => ({
      supported_languages: ["en"],
      default_target_language: "en",
    })),
    put: vi.fn(),
    post: vi.fn(),
    delete: vi.fn(),
  },
  authFetch: vi.fn(),
  API_BASE: "",
}));

// ---------------------------------------------------------------------------
// Component under test (imported AFTER mocks).
// ---------------------------------------------------------------------------

import { WikiTab } from "../WikiTab";

function renderTab() {
  return render(
    <MemoryRouter initialEntries={["/channels/c1/wiki"]}>
      <Routes>
        <Route path="/channels/:id/wiki" element={<WikiTab />} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("WikiTab — empty-state smoke test", () => {
  it("renders the empty-state without crashing when channel has no memories", async () => {
    expect(() => renderTab()).not.toThrow();
    await waitFor(() => {
      // ``useWiki`` mock returns ``isNotFound: true`` so the 404
      // ``PipelineEmptyState`` branch (WikiTab.tsx:905) renders with
      // title "Build your channel wiki". The test's value is the
      // no-crash assertion above; the text match below is just
      // confirmation that the empty-state branch was rendered (not
      // the loading skeleton or an unrelated error path).
      expect(screen.getByText(/build your channel wiki/i)).toBeInTheDocument();
    });
  });
});
