/**
 * Tests for TopicPage's adaptive-modules dispatch contract.
 *
 * Covers the spec scenarios:
 * - "Topic page with modules renders module-by-module" — non-empty
 *   `page.modules` routes through ModuleRenderer, NOT WikiMarkdown
 *   over page.content
 * - "Topic page with empty modules falls back to legacy markdown
 *   rendering" — empty `page.modules` keeps today's WikiMarkdown
 *   flow intact
 * - Header chip row renders memories + last-updated + citations chips
 *   when data is present, and collapses chips for missing data.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

// ---------------------------------------------------------------------------
// Mock heavy children so the test stays focused on the dispatch contract.
// WikiMarkdown gets a sentinel marker so we can detect when it mounted.
// ---------------------------------------------------------------------------

vi.mock("../WikiMarkdown", () => ({
  WikiMarkdown: ({ content }: { content: string }) => (
    <div data-testid="legacy-wikimarkdown">{content.slice(0, 40)}</div>
  ),
}));

vi.mock("../CitationPanel", () => ({
  CitationPanel: () => <div data-testid="citation-panel" />,
}));

vi.mock("../TensionsSection", () => ({
  TensionsSection: () => null,
}));

// Each module renderer renders a sentinel testid so we can detect
// which ones mounted. ModuleRenderer is real — we want the real
// switch dispatching.
vi.mock("../modules/MarkdownModule", () => ({
  MarkdownModule: ({ module }: { module: { id: string; anchor: string } }) => (
    <div data-testid={`module-${module.id}`} data-anchor={module.anchor} />
  ),
}));
// ``key_facts`` v2 is a frontend renderer (not markdown-based) — mock
// it for the same dispatch sentinel so this dispatch test stays
// focused on the switch contract, not the component's internals.
vi.mock("../modules/KeyFactsModule", () => ({
  KeyFactsModule: ({ module }: { module: { id: string; anchor: string } }) => (
    <div data-testid={`module-${module.id}`} data-anchor={module.anchor} />
  ),
}));

import { TopicPage } from "../TopicPage";
import type { WikiPage } from "@/lib/types";

function makePage(overrides: Partial<WikiPage> = {}): WikiPage {
  return {
    id: "topic-auth",
    slug: "topic-auth",
    title: "Authentication",
    page_type: "topic",
    parent_id: null,
    section_number: "1.1",
    content: "Legacy markdown content body here.",
    summary: "",
    memory_count: 47,
    last_updated: new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString(), // 2h ago
    citations: [],
    children: [],
    modules: [],
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
});

function renderTopicPage(page: WikiPage) {
  return render(
    <MemoryRouter>
      <TopicPage page={page} onNavigate={vi.fn()} />
    </MemoryRouter>,
  );
}

describe("TopicPage — adaptive modules dispatch", () => {
  it("renders ModuleRenderer when page.modules is non-empty (does NOT mount legacy WikiMarkdown over content)", () => {
    const page = makePage({
      modules: [
        { id: "key_facts", anchor: "kf1" },
        { id: "decision_log", anchor: "dl1" },
      ],
    });
    renderTopicPage(page);

    // Module dispatcher mounted the right components in body order.
    expect(screen.getByTestId("module-key_facts")).toBeInTheDocument();
    expect(screen.getByTestId("module-decision_log")).toBeInTheDocument();
    // Legacy WikiMarkdown over page.content did NOT mount.
    expect(screen.queryByTestId("legacy-wikimarkdown")).not.toBeInTheDocument();
  });

  it("falls back to WikiMarkdown over page.content when page.modules is empty (legacy backward-compat)", () => {
    const page = makePage({ modules: [] });
    renderTopicPage(page);

    expect(screen.getByTestId("legacy-wikimarkdown")).toBeInTheDocument();
    expect(screen.queryByTestId("module-key_facts")).not.toBeInTheDocument();
  });

  it("falls back to WikiMarkdown when page.modules is undefined (legacy persisted page)", () => {
    const page = makePage({ modules: undefined });
    renderTopicPage(page);
    expect(screen.getByTestId("legacy-wikimarkdown")).toBeInTheDocument();
  });

  it("preserves the order of modules from the plan", () => {
    const page = makePage({
      modules: [
        { id: "decision_log", anchor: "dl1" },
        { id: "key_facts", anchor: "kf1" },
        { id: "open_questions", anchor: "oq1" },
      ],
    });
    renderTopicPage(page);

    const rendered = [
      screen.getByTestId("module-decision_log"),
      screen.getByTestId("module-key_facts"),
      screen.getByTestId("module-open_questions"),
    ];
    // DOM order matches plan order.
    for (let i = 0; i < rendered.length - 1; i++) {
      expect(
        rendered[i].compareDocumentPosition(rendered[i + 1]) &
          Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
    }
  });

  it("renders the header chip row with memories and last-updated chips", () => {
    const page = makePage({ memory_count: 47 });
    renderTopicPage(page);

    expect(screen.getByText("47")).toBeInTheDocument();
    expect(screen.getByText(/memories/)).toBeInTheDocument();
    expect(screen.getByText(/updated/)).toBeInTheDocument();
  });

  it("collapses optional chips when data is missing", () => {
    const page = makePage({
      citations: [],
      last_updated: "",
    });
    renderTopicPage(page);

    // Memories chip always renders.
    expect(screen.getByText(/memories|memory/)).toBeInTheDocument();
    // Citations chip absent.
    expect(screen.queryByText(/citation/)).not.toBeInTheDocument();
    // Updated chip absent (no parsable timestamp).
    expect(screen.queryByText(/updated/)).not.toBeInTheDocument();
  });

  it("renders sub-topic breadcrumb when page is a sub-topic", () => {
    const page = makePage({
      page_type: "sub-topic",
      parent_id: "topic-parent",
    });
    renderTopicPage(page);
    // Breadcrumb mounts.
    expect(screen.getByText(/parent/)).toBeInTheDocument();
  });
});
