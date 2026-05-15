/**
 * Tests for the ModuleRenderer dispatcher — narrative-aware appendix
 * layout (`wiki-narrative-articles` change, Phase 3).
 *
 * Coverage:
 *  - With narrative_article + other modules: NarrativeArticleModule
 *    renders FIRST, other spine modules render inside the
 *    "Reference & Evidence" appendix
 *  - Without narrative_article: existing modules render at top, no
 *    appendix wrapper
 *  - Appendix wrapper exposes the supporting-module count in the label
 *  - Reading aids (acronym_legend + provenance_drawer) keep their
 *    existing footer block in both layouts
 */

import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { ModuleRenderer } from "../ModuleRenderer";
import type { WikiCitation, WikiPageModule } from "@/lib/types";

afterEach(() => cleanup());

// MermaidBlock relies on browser APIs not present in jsdom — mock so
// any module that pulls it in transitively (FlowChartModule etc.)
// still renders without crashing.
vi.mock("../../MermaidBlock", () => ({
  MermaidBlock: ({ chart }: { chart: string }) => (
    <div data-testid="mock-mermaid-block">{chart}</div>
  ),
}));

function makeNarrativeModule(): WikiPageModule {
  return {
    id: "narrative_article",
    anchor: "narrative",
    data: {
      label: "Article",
      renderer_kind: "frontend",
      sections: [
        {
          anchor: "intro",
          heading: "Introduction",
          paragraphs: [
            { text: "Atlas connects memories to a wiki.", citations: ["f1"], is_inference: false },
          ],
          citations: ["f1"],
          visual: null,
          citation_coverage: 1,
        },
      ],
    },
  };
}

function makeHeroModule(): WikiPageModule {
  return {
    id: "hero_summary",
    anchor: "hero",
    data: {
      label: "Summary",
      renderer_kind: "frontend",
      tldr: "**Key insight goes here.**",
      summary: "Two-sentence overview.",
      highlights: { critical_count: 1 },
    },
  };
}

function makeKeyFactsModule(): WikiPageModule {
  return {
    id: "key_facts",
    anchor: "key-facts",
    data: {
      label: "Key facts",
      // Markdown body — KeyFactsModule renders the markdown via
      // WikiMarkdown. We don't assert on its content; we only need
      // the module to mount without crashing.
      markdown: "## Key facts\n\n- Fact one [f1]",
      citations: ["f1"],
    },
  };
}

function makeAcronymLegendModule(): WikiPageModule {
  return {
    id: "acronym_legend",
    anchor: "acronym",
    data: {
      label: "Terms used on this page",
      renderer_kind: "frontend",
      items: [{ term: "MFA", definition: "Multi-Factor Authentication" }],
    },
  };
}

const citations: WikiCitation[] = [
  {
    id: "f1",
    author: "Alice",
    channel: "general",
    timestamp: "2025-04-01 12:34",
    text_excerpt: "Memory text",
    permalink: "https://example.com/m/123",
  },
];

const noop = () => undefined;

// ---------------------------------------------------------------------------

describe("ModuleRenderer — module-as-appendix layout", () => {
  it("renders NarrativeArticleModule first when narrative_article is present", () => {
    render(
      <ModuleRenderer
        modules={[makeHeroModule(), makeNarrativeModule(), makeKeyFactsModule()]}
        citations={citations}
        onNavigate={noop}
      />,
    );
    // Article body is mounted at the top of the page.
    expect(screen.getByTestId("narrative-article")).toBeInTheDocument();
    // Other spine modules sit inside the appendix.
    const appendix = screen.getByTestId("wiki-reference-evidence");
    expect(appendix).toBeInTheDocument();
    // The hero summary lives inside the appendix, not before the article.
    expect(appendix.contains(screen.getByTestId("module-hero_summary"))).toBe(true);
  });

  it("appendix label reports the count of supporting (non-narrative, non-aid) modules", () => {
    render(
      <ModuleRenderer
        modules={[
          makeNarrativeModule(),
          makeHeroModule(),
          makeKeyFactsModule(),
        ]}
        citations={citations}
        onNavigate={noop}
      />,
    );
    const appendix = screen.getByTestId("wiki-reference-evidence");
    // 2 supporting modules: hero_summary + key_facts
    const summary = appendix.querySelector("summary");
    expect(summary?.textContent || "").toMatch(/Reference\s*&\s*Evidence\s*\(\s*2\s+supporting\s+modules\s*\)/);
  });

  it("appendix is open by default (so users see the modules without expanding)", () => {
    render(
      <ModuleRenderer
        modules={[makeNarrativeModule(), makeHeroModule()]}
        citations={citations}
        onNavigate={noop}
      />,
    );
    const appendix = screen.getByTestId("wiki-reference-evidence") as HTMLDetailsElement;
    expect(appendix.open).toBe(true);
  });

  it("renders no appendix wrapper when narrative_article is ABSENT (legacy module-only)", () => {
    render(
      <ModuleRenderer
        modules={[makeHeroModule(), makeKeyFactsModule()]}
        citations={citations}
        onNavigate={noop}
      />,
    );
    expect(screen.queryByTestId("wiki-reference-evidence")).not.toBeInTheDocument();
    // Hero still mounts at the top of the page.
    expect(screen.getByTestId("module-hero_summary")).toBeInTheDocument();
  });

  it("preserves the reading-aids footer block alongside the appendix", () => {
    render(
      <ModuleRenderer
        modules={[
          makeNarrativeModule(),
          makeHeroModule(),
          makeAcronymLegendModule(),
        ]}
        citations={citations}
        onNavigate={noop}
      />,
    );
    // Reading-aids footer is its own <aside> below the appendix.
    expect(screen.getByTestId("wiki-page-footer")).toBeInTheDocument();
    // Article still renders at the top.
    expect(screen.getByTestId("narrative-article")).toBeInTheDocument();
    // Appendix renders for the remaining spine modules.
    expect(screen.getByTestId("wiki-reference-evidence")).toBeInTheDocument();
  });
});
