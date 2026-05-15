/**
 * Tests for the Hero Summary module — page header renderer.
 *
 * Coverage:
 *  - TL;DR rendering (bold styling, markdown bold markers stripped)
 *  - Summary prose rendering
 *  - Highlight stat chip dropping when count is 0
 *  - Singular vs plural label rendering
 *  - Empty payload returns null (no header at all)
 */

import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { HeroSummaryModule } from "../HeroSummaryModule";
import type { WikiPageModule } from "@/lib/types";

afterEach(() => cleanup());

interface HeroFixture {
  tldr?: string;
  summary?: string;
  highlights?: {
    critical_count?: number;
    decision_count?: number;
    open_question_count?: number;
    tension_count?: number;
  };
}

function makeModule(fixture: HeroFixture = {}): WikiPageModule {
  return {
    id: "hero_summary",
    anchor: "hero",
    data: {
      label: "Summary",
      renderer_kind: "frontend",
      tldr: fixture.tldr ?? "",
      summary: fixture.summary ?? "",
      highlights: fixture.highlights ?? {
        critical_count: 0,
        decision_count: 0,
        open_question_count: 0,
        tension_count: 0,
      },
    },
  };
}

const noop = () => undefined;

// ---------------------------------------------------------------------------
// TL;DR rendering
// ---------------------------------------------------------------------------

describe("HeroSummaryModule — TL;DR", () => {
  it("renders the TL;DR text in the bold header element", () => {
    render(
      <HeroSummaryModule
        module={makeModule({
          tldr: "**JWT replaces SAML for service auth.**",
        })}
        citations={[]}
        onNavigate={noop}
      />,
    );
    const tldr = screen.getByTestId("hero-summary-tldr");
    expect(tldr).toBeInTheDocument();
    // Markdown bold markers stripped — the styling makes it bold.
    expect(tldr.textContent).toBe("JWT replaces SAML for service auth.");
  });

  it("renders the summary prose under the TL;DR", () => {
    render(
      <HeroSummaryModule
        module={makeModule({
          tldr: "**X.**",
          summary: "Two-sentence overview goes here. Done.",
        })}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(screen.getByText(/Two-sentence overview/)).toBeInTheDocument();
  });

  it("renders nothing when both tldr and summary are empty", () => {
    const { container } = render(
      <HeroSummaryModule
        module={makeModule({ tldr: "", summary: "" })}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(container.firstChild).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Highlight stat chips
// ---------------------------------------------------------------------------

describe("HeroSummaryModule — highlight stat chips", () => {
  it("renders all 4 stat chips when every count is > 0", () => {
    render(
      <HeroSummaryModule
        module={makeModule({
          tldr: "**X.**",
          highlights: {
            critical_count: 2,
            decision_count: 4,
            open_question_count: 1,
            tension_count: 3,
          },
        })}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(screen.getByTestId("hero-stat-critical")).toBeInTheDocument();
    expect(screen.getByTestId("hero-stat-decision")).toBeInTheDocument();
    expect(screen.getByTestId("hero-stat-open_question")).toBeInTheDocument();
    expect(screen.getByTestId("hero-stat-tension")).toBeInTheDocument();
  });

  it("drops the critical chip when critical_count is 0", () => {
    render(
      <HeroSummaryModule
        module={makeModule({
          tldr: "**X.**",
          highlights: {
            critical_count: 0,
            decision_count: 2,
            open_question_count: 0,
            tension_count: 0,
          },
        })}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(screen.queryByTestId("hero-stat-critical")).not.toBeInTheDocument();
    expect(screen.getByTestId("hero-stat-decision")).toBeInTheDocument();
  });

  it("drops every chip when all counts are 0 (no stat strip at all)", () => {
    render(
      <HeroSummaryModule
        module={makeModule({
          tldr: "**X.**",
          summary: "Y.",
          highlights: {
            critical_count: 0,
            decision_count: 0,
            open_question_count: 0,
            tension_count: 0,
          },
        })}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(screen.queryByTestId("hero-summary-stats")).not.toBeInTheDocument();
  });

  it("uses singular labels for count == 1 and plural otherwise", () => {
    const { rerender } = render(
      <HeroSummaryModule
        module={makeModule({
          tldr: "**X.**",
          highlights: { decision_count: 1 },
        })}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(screen.getByText(/decision\b/)).toBeInTheDocument();
    expect(screen.queryByText(/decisions/)).not.toBeInTheDocument();

    rerender(
      <HeroSummaryModule
        module={makeModule({
          tldr: "**X.**",
          highlights: { decision_count: 4 },
        })}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(screen.getByText(/decisions/)).toBeInTheDocument();
  });
});
