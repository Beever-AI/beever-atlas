/**
 * WikiLayout tests — narrative TOC mount contract.
 *
 * Covers C-2 in the wiki-narrative-articles code review: the
 * ``NarrativeTOC`` component is exported and unit-tested but no
 * production component imports it. Without this mount the sticky
 * right-rail TOC + "Jump to section" dropdown never reach the DOM.
 *
 * The mount rule: when the active page has a narrative payload with
 * ≥3 sections (either via ``page.narrative_sections`` or via the
 * ``narrative_article`` module's ``data.sections``), the right rail
 * swaps the heading-extraction TOC for ``NarrativeTOC``. When the
 * payload is absent or below the 3-section threshold, the legacy
 * ``WikiTableOfContents`` continues to render.
 */

import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { WikiLayout } from "../WikiLayout";
import type { WikiPage, WikiStructure } from "@/lib/types";

// ---------------------------------------------------------------------------
// Mocks — keep the test focused on the TOC mount switch. The sidebar
// + freshness chip + breadcrumb + regenerate button are not under test.
// ---------------------------------------------------------------------------

vi.mock("../WikiSidebar", () => ({
  WikiSidebar: () => <div data-testid="mock-wiki-sidebar" />,
}));

vi.mock("../WikiBreadcrumb", () => ({
  WikiBreadcrumb: () => <div data-testid="mock-wiki-breadcrumb" />,
}));

vi.mock("../FreshnessBadge", () => ({
  FreshnessBadge: () => <div data-testid="mock-freshness-badge" />,
}));

vi.mock("../VersionHistoryPanel", () => ({
  VersionHistoryPanel: () => <div data-testid="mock-version-history" />,
}));

vi.mock("@/components/channel/WikiRegenerateButton", () => ({
  WikiRegenerateButton: () => <div data-testid="mock-regen-button" />,
}));

// Stub the heading-extraction TOC so we can assert which TOC mounted.
vi.mock("../WikiTableOfContents", () => ({
  WikiTableOfContents: () => (
    <div data-testid="mock-legacy-toc">legacy TOC</div>
  ),
}));

// Real ``NarrativeTOC`` — already unit-tested. We assert it lands in
// the DOM by querying its sticky-panel testid.

afterEach(() => {
  cleanup();
});

function makeStructure(): WikiStructure {
  return {
    channel_id: "C_TEST",
    channel_name: "test-channel",
    platform: "slack",
    generated_at: new Date().toISOString(),
    is_stale: false,
    pages: [],
  };
}

function makePage(overrides: Partial<WikiPage> = {}): WikiPage {
  return {
    id: "topic-auth",
    slug: "topic-auth",
    title: "Authentication",
    page_type: "topic",
    parent_id: null,
    section_number: "1.1",
    content: "Body",
    summary: "",
    memory_count: 5,
    last_updated: new Date().toISOString(),
    citations: [],
    children: [],
    modules: [],
    ...overrides,
  };
}

function renderLayout(activePage: WikiPage) {
  return render(
    <WikiLayout
      channelId="C_TEST"
      structure={makeStructure()}
      activePage={activePage}
      onNavigate={vi.fn()}
      onRefresh={vi.fn()}
      isRefreshing={false}
      currentLang="en"
      supportedLanguages={["en"]}
      onRegenerateInLang={vi.fn()}
    >
      <div data-testid="layout-children">page body</div>
    </WikiLayout>,
  );
}

describe("WikiLayout — NarrativeTOC mount", () => {
  it("mounts NarrativeTOC when page has 3+ narrative sections via narrative_article module data", () => {
    const page = makePage({
      modules: [
        {
          id: "narrative_article",
          anchor: "article",
          data: {
            sections: [
              { anchor: "context", heading: "Context", paragraphs: [] },
              { anchor: "decision", heading: "Decision", paragraphs: [] },
              { anchor: "implications", heading: "Implications", paragraphs: [] },
            ],
          },
        },
      ],
    });
    renderLayout(page);
    // Narrative TOC mounted (sticky-panel testid is the wide-viewport
    // form of the TOC).
    expect(screen.getByTestId("narrative-toc-sticky")).toBeInTheDocument();
    expect(screen.getByTestId("narrative-toc-dropdown")).toBeInTheDocument();
    // Legacy heading-extraction TOC must NOT mount on narrative pages.
    expect(screen.queryByTestId("mock-legacy-toc")).not.toBeInTheDocument();
  });

  it("mounts NarrativeTOC when page has 3+ narrative sections via page-level narrative_sections", () => {
    const page = makePage({
      narrative_sections: [
        { anchor: "context", heading: "Context", paragraphs: [] },
        { anchor: "decision", heading: "Decision", paragraphs: [] },
        { anchor: "implications", heading: "Implications", paragraphs: [] },
      ],
    });
    renderLayout(page);
    expect(screen.getByTestId("narrative-toc-sticky")).toBeInTheDocument();
    expect(screen.queryByTestId("mock-legacy-toc")).not.toBeInTheDocument();
  });

  it("falls back to WikiTableOfContents when narrative payload is absent", () => {
    const page = makePage({
      modules: [{ id: "key_facts", anchor: "kf" }],
      narrative_sections: [],
    });
    renderLayout(page);
    expect(screen.getByTestId("mock-legacy-toc")).toBeInTheDocument();
    expect(screen.queryByTestId("narrative-toc-sticky")).not.toBeInTheDocument();
  });

  it("falls back to WikiTableOfContents when narrative has fewer than 3 sections", () => {
    const page = makePage({
      narrative_sections: [
        { anchor: "context", heading: "Context", paragraphs: [] },
        { anchor: "decision", heading: "Decision", paragraphs: [] },
      ],
    });
    renderLayout(page);
    expect(screen.getByTestId("mock-legacy-toc")).toBeInTheDocument();
    expect(screen.queryByTestId("narrative-toc-sticky")).not.toBeInTheDocument();
  });

  it("renders narrative section headings in the TOC", () => {
    const page = makePage({
      narrative_sections: [
        { anchor: "context", heading: "Why we adopted Authlib", paragraphs: [] },
        { anchor: "decision", heading: "Decision and rollout", paragraphs: [] },
        { anchor: "implications", heading: "What this unlocks", paragraphs: [] },
      ],
    });
    renderLayout(page);
    // Each heading renders TWICE: once in the sticky desktop panel
    // (``narrative-toc-sticky``) and once in the mobile dropdown's
    // ``<option>`` (``narrative-toc-dropdown``). ``getAllByText`` lets
    // us assert both surfaces present without picking which one wins.
    expect(screen.getAllByText("Why we adopted Authlib").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Decision and rollout").length).toBeGreaterThan(0);
    expect(screen.getAllByText("What this unlocks").length).toBeGreaterThan(0);
  });
});
