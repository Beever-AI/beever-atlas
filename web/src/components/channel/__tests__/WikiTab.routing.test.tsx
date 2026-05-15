/**
 * WikiTab — path-based routing.
 *
 * Verifies the route shapes the wiki tab now consumes:
 *   /channels/:id/wiki                  → overview
 *   /channels/:id/wiki/:slug            → resolved topic / folder page
 *   /channels/:id/wiki?page=<id>        → legacy redirect (scrubbed on
 *                                          mount, mapped id → slug, query
 *                                          params other than ``page``
 *                                          preserved)
 *
 * The wiki document is mocked with two pages so we can drive the
 * slug→id and id→slug map paths without standing up real fixtures.
 *
 * Heavy children (WikiLayout, WikiSidebar, mermaid, the version-history
 * panel) are NOT mocked; they're rendered top-to-bottom so any
 * regression in how they consume the new URL shape surfaces here.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen, waitFor, cleanup, fireEvent } from "@testing-library/react";
import { MemoryRouter, Routes, Route, useLocation } from "react-router-dom";

// ---------------------------------------------------------------------------
// Hook mocks. Every mock is hoisted by vi.mock so the test reads them
// before the component-under-test imports the same modules.
// ---------------------------------------------------------------------------

const overviewPage = {
  id: "overview",
  slug: "overview",
  title: "Channel Overview",
  page_type: "fixed" as const,
  parent_id: null,
  section_number: "0",
  content: "# Channel Overview\n\nWelcome to the overview body text.",
  summary: "",
  memory_count: 10,
  last_updated: "2026-05-01T00:00:00Z",
  citations: [],
  children: [],
  modules: [],
};

const topicXPage = {
  id: "topic-x",
  slug: "topic-x",
  title: "Topic X Title",
  page_type: "topic" as const,
  parent_id: null,
  section_number: "1.1",
  content: "# Topic X Title\n\nTopic X body content goes here.",
  summary: "",
  memory_count: 5,
  last_updated: "2026-05-01T00:00:00Z",
  citations: [],
  children: [],
  modules: [],
};

const topicYPage = {
  id: "topic-y",
  slug: "topic-y",
  title: "Topic Y Title",
  page_type: "topic" as const,
  parent_id: null,
  section_number: "1.2",
  content: "# Topic Y Title\n\nTopic Y body content goes here.",
  summary: "",
  memory_count: 3,
  last_updated: "2026-05-01T00:00:00Z",
  citations: [],
  children: [],
  modules: [],
};

const topicZPage = {
  id: "topic-z",
  slug: "topic-z",
  title: "Topic Z Title",
  page_type: "topic" as const,
  parent_id: null,
  section_number: "1.3",
  content: "# Topic Z Title\n\nTopic Z body.",
  summary: "",
  memory_count: 2,
  last_updated: "2026-05-01T00:00:00Z",
  citations: [],
  children: [],
  modules: [],
};

const wikiFixture = {
  channel_id: "c1",
  channel_name: "Test Channel",
  platform: "slack",
  generated_at: "2026-05-01T00:00:00Z",
  is_stale: false,
  structure: {
    channel_id: "c1",
    channel_name: "Test Channel",
    platform: "slack",
    generated_at: "2026-05-01T00:00:00Z",
    is_stale: false,
    pages: [
      {
        id: "overview",
        title: "Channel Overview",
        slug: "overview",
        section_number: "0",
        page_type: "fixed" as const,
        memory_count: 10,
        children: [],
      },
      {
        id: "topic-x",
        title: "Topic X Title",
        slug: "topic-x",
        section_number: "1.1",
        page_type: "topic" as const,
        memory_count: 5,
        children: [],
      },
      {
        id: "topic-y",
        title: "Topic Y Title",
        slug: "topic-y",
        section_number: "1.2",
        page_type: "topic" as const,
        memory_count: 3,
        children: [],
      },
      {
        id: "topic-z",
        title: "Topic Z Title",
        slug: "topic-z",
        section_number: "1.3",
        page_type: "topic" as const,
        memory_count: 2,
        children: [],
      },
    ],
  },
  overview: overviewPage,
  metadata: {
    member_count: 1,
    message_count: 1,
    memory_count: 10,
    entity_count: 0,
    media_count: 0,
    page_count: 4,
    generation_cost_usd: 0,
    generation_duration_ms: 0,
  },
  version_count: 0,
};

vi.mock("@/hooks/useWiki", () => ({
  useWiki: () => ({
    data: wikiFixture,
    isLoading: false,
    error: null,
    isNotFound: false,
    refetch: vi.fn(),
  }),
}));

vi.mock("@/hooks/useChannelMemoryCount", () => ({
  useChannelMemoryCount: () => ({
    hasMemories: true,
    isLoading: false,
    refetch: vi.fn(),
  }),
}));

vi.mock("@/hooks/useWikiPage", () => ({
  // Resolve lazy non-overview pages from the fixture map. The hook is
  // called with ``pageId`` ∈ {topic-x, topic-y, topic-z, undefined}; we
  // return the matching fixture so the page body actually renders.
  useWikiPage: (_channelId: string | undefined, pageId?: string) => {
    if (!pageId) return { data: null, isLoading: false, isRevalidating: false };
    if (pageId === "topic-x") return { data: topicXPage, isLoading: false, isRevalidating: false };
    if (pageId === "topic-y") return { data: topicYPage, isLoading: false, isRevalidating: false };
    if (pageId === "topic-z") return { data: topicZPage, isLoading: false, isRevalidating: false };
    return { data: null, isLoading: false, isRevalidating: false };
  },
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

// Spy that captures the current location every render so the test can
// assert the URL after navigation events fire.
function LocationSpy() {
  const location = useLocation();
  return (
    <div
      data-testid="location-spy"
      data-pathname={location.pathname}
      data-search={location.search}
    />
  );
}

function renderAt(initialEntry: string) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route
          path="/channels/:id/wiki"
          element={
            <>
              <WikiTab />
              <LocationSpy />
            </>
          }
        />
        <Route
          path="/channels/:id/wiki/:slug"
          element={
            <>
              <WikiTab />
              <LocationSpy />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  // jsdom — the wiki layout reads localStorage for sidebar width;
  // wipe so tests don't leak state across runs.
  try {
    window.localStorage.clear();
  } catch {
    /* private-mode safari noop */
  }
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("WikiTab — path-based routing", () => {
  it("renders the overview page when mounted at /channels/:id/wiki", async () => {
    renderAt("/channels/c1/wiki");
    await waitFor(() => {
      // Overview heading sits inside the rendered page body. Use a
      // role+name match so the breadcrumb's "Wiki" label doesn't
      // collide with the page title heading.
      expect(
        screen.getByRole("heading", { level: 1, name: /channel overview/i }),
      ).toBeInTheDocument();
    });
    const spy = screen.getByTestId("location-spy");
    expect(spy.getAttribute("data-pathname")).toBe("/channels/c1/wiki");
  });

  it("renders topic-x when mounted at /channels/:id/wiki/topic-x", async () => {
    renderAt("/channels/c1/wiki/topic-x");
    await waitFor(() => {
      expect(
        screen.getByRole("heading", { level: 1, name: /topic x title/i }),
      ).toBeInTheDocument();
    });
    const spy = screen.getByTestId("location-spy");
    expect(spy.getAttribute("data-pathname")).toBe("/channels/c1/wiki/topic-x");
  });

  it("scrubs legacy ?page=<id> and replaces with /channels/:id/wiki/:slug", async () => {
    renderAt("/channels/c1/wiki?page=topic-y");
    await waitFor(() => {
      const spy = screen.getByTestId("location-spy");
      expect(spy.getAttribute("data-pathname")).toBe(
        "/channels/c1/wiki/topic-y",
      );
      // The ``page`` param has been stripped from the search string.
      expect(spy.getAttribute("data-search") ?? "").not.toMatch(/page=/);
    });
  });

  it("scrubs ?page=<unknown-id> back to /channels/:id/wiki without crashing", async () => {
    renderAt("/channels/c1/wiki?page=unknown-id");
    await waitFor(() => {
      const spy = screen.getByTestId("location-spy");
      // Path stays on the overview (no slug) — the legacy id was
      // unknown so the scrub falls back to dropping the param.
      expect(spy.getAttribute("data-pathname")).toBe("/channels/c1/wiki");
      expect(spy.getAttribute("data-search") ?? "").not.toMatch(/page=/);
    });
    // Overview still renders — the scrub did not 404-loop.
    expect(
      screen.getByRole("heading", { level: 1, name: /channel overview/i }),
    ).toBeInTheDocument();
  });

  it("preserves ?view=graph and ?lang=ja when mounted on a slug path", async () => {
    renderAt("/channels/c1/wiki/topic-x?view=graph&lang=ja");
    await waitFor(() => {
      const spy = screen.getByTestId("location-spy");
      expect(spy.getAttribute("data-pathname")).toBe(
        "/channels/c1/wiki/topic-x",
      );
      const search = spy.getAttribute("data-search") ?? "";
      // Both query params survive the path. ``view=graph`` flips
      // WikiTab into graph mode (no page heading rendered) so we
      // assert the URL state directly here rather than the body.
      expect(search).toMatch(/view=graph/);
      expect(search).toMatch(/lang=ja/);
    });
  });

  it("preserves ?version=N across slug navigations and on cold mount", async () => {
    // Cold mount on a topic page with ``?version=3`` — the version
    // state must come from the URL, not local React state. Refreshing
    // a deep link or sharing one used to drop the version because
    // ``viewingVersionNumber`` was a ``useState`` value.
    renderAt("/channels/c1/wiki/topic-x?version=3");
    await waitFor(() => {
      const spy = screen.getByTestId("location-spy");
      expect(spy.getAttribute("data-pathname")).toBe("/channels/c1/wiki/topic-x");
      // ``version=3`` survives the mount unchanged.
      expect(spy.getAttribute("data-search") ?? "").toMatch(/version=3/);
    });
  });

  it("clicking a topic card on the overview navigates to its slug path", async () => {
    renderAt("/channels/c1/wiki");
    await waitFor(() => {
      expect(
        screen.getByRole("heading", { level: 1, name: /channel overview/i }),
      ).toBeInTheDocument();
    });
    // The OverviewPage's "All Topics" grid renders TopicCard buttons
    // for each topic; the card text is the topic title. Click "Topic Z
    // Title" and assert the URL flips to its slug path.
    const card = screen.getAllByText(/topic z title/i)[0];
    fireEvent.click(card);
    await waitFor(() => {
      const spy = screen.getByTestId("location-spy");
      expect(spy.getAttribute("data-pathname")).toBe(
        "/channels/c1/wiki/topic-z",
      );
    });
  });
});
