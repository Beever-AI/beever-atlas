/**
 * Tests for the NarrativeArticleModule.
 *
 * Coverage:
 *  - Single section + paragraph renders the body
 *  - Multiple sections render with anchor IDs as `id={anchor}`
 *  - Citation chips render with `data-fact-id` attribute
 *  - Citation chip exposes the fact text via the `title` attribute (the
 *    hover popover is rendered alongside, but title is the zero-cost
 *    fallback for screen readers / mobile)
 *  - agent-inference paragraph renders the `[agent-inference]` chip
 *  - Visual dispatch: table, list, callout, code, blockquote, mermaid
 *    each render the matching test-id wrapper
 *  - Reading-time estimate matches `ceil(words / 200)`
 *  - "X memories synthesized" badge counts distinct fact_ids
 *  - Empty sections array renders nothing
 *  - Citation chip without a matching `WikiCitation` still renders
 *    (back-end may emit a fact_id with no popover preview)
 */

import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { NarrativeArticleModule } from "../NarrativeArticleModule";
import type { WikiCitation, WikiPageModule } from "@/lib/types";

afterEach(() => cleanup());

// MermaidBlock pulls in mermaid which is heavy + relies on browser
// APIs (matchMedia, getComputedStyle on SVG elements). Mock it for
// the purpose of these tests — we only assert that the wrapper
// renders, not the diagram pixels themselves.
vi.mock("../../MermaidBlock", () => ({
  MermaidBlock: ({ chart }: { chart: string }) => (
    <div data-testid="mock-mermaid-block">{chart}</div>
  ),
}));

interface ParagraphFixture {
  text: string;
  citations?: string[];
  is_inference?: boolean;
}

interface SectionFixture {
  anchor: string;
  heading: string;
  paragraphs: ParagraphFixture[];
  citations?: string[];
  visual?: { kind: string; content: unknown } | null;
  citation_coverage?: number;
}

function makeModule(sections: SectionFixture[] = []): WikiPageModule {
  return {
    id: "narrative_article",
    anchor: "narrative",
    data: {
      label: "Article",
      renderer_kind: "frontend",
      sections: sections.map((s) => ({
        anchor: s.anchor,
        heading: s.heading,
        paragraphs: s.paragraphs.map((p) => ({
          text: p.text,
          citations: p.citations ?? [],
          is_inference: p.is_inference ?? false,
        })),
        citations: s.citations ?? [],
        visual: s.visual ?? null,
        citation_coverage: s.citation_coverage ?? 1,
      })),
    },
  };
}

function makeCitation(id: string, overrides: Partial<WikiCitation> = {}): WikiCitation {
  return {
    id,
    author: "Alice",
    channel: "general",
    timestamp: "2025-04-01 12:34",
    text_excerpt: `Memory text for ${id}`,
    permalink: "https://example.com/m/123",
    ...overrides,
  };
}

const noop = () => undefined;

// ---------------------------------------------------------------------------
// Basic rendering
// ---------------------------------------------------------------------------

describe("NarrativeArticleModule — basic rendering", () => {
  it("renders article with a single section + paragraph", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "intro",
            heading: "Introduction",
            paragraphs: [
              { text: "Beever Atlas connects Slack memories to a wiki.", citations: ["f1"] },
            ],
          },
        ])}
        citations={[makeCitation("f1")]}
        onNavigate={noop}
      />,
    );
    expect(screen.getByTestId("narrative-article")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Introduction" })).toBeInTheDocument();
    expect(
      screen.getByText(/Beever Atlas connects Slack memories to a wiki\./),
    ).toBeInTheDocument();
  });

  it("renders nothing when sections array is empty (defensive)", () => {
    const { container } = render(
      <NarrativeArticleModule
        module={makeModule([])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders multiple sections with anchor IDs as `id` attributes", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "intro",
            heading: "Introduction",
            paragraphs: [{ text: "Para A.", citations: ["f1"] }],
          },
          {
            anchor: "details",
            heading: "Details",
            paragraphs: [{ text: "Para B.", citations: ["f2"] }],
          },
          {
            anchor: "outlook",
            heading: "Outlook",
            paragraphs: [{ text: "Para C.", citations: ["f3"] }],
          },
        ])}
        citations={[makeCitation("f1"), makeCitation("f2"), makeCitation("f3")]}
        onNavigate={noop}
      />,
    );
    const sections = screen.getAllByTestId("narrative-section");
    expect(sections).toHaveLength(3);
    // The h2 heading carries the anchor id so TOC links scroll-snap.
    expect(document.getElementById("intro")).not.toBeNull();
    expect(document.getElementById("details")).not.toBeNull();
    expect(document.getElementById("outlook")).not.toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Citation chips
// ---------------------------------------------------------------------------

describe("NarrativeArticleModule — citation chips", () => {
  it("renders chips with `data-fact-id` attribute matching the cited fact_id", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "intro",
            heading: "Intro",
            paragraphs: [{ text: "Some prose.", citations: ["f-alpha", "f-beta"] }],
          },
        ])}
        citations={[
          makeCitation("f-alpha", { text_excerpt: "Alpha memory" }),
          makeCitation("f-beta", { text_excerpt: "Beta memory" }),
        ]}
        onNavigate={noop}
      />,
    );
    const chips = screen.getAllByTestId("narrative-citation-chip");
    expect(chips).toHaveLength(2);
    expect(chips[0].getAttribute("data-fact-id")).toBe("f-alpha");
    expect(chips[1].getAttribute("data-fact-id")).toBe("f-beta");
    // Display indices are 1-indexed in order of first occurrence.
    expect(chips[0].textContent).toBe("[1]");
    expect(chips[1].textContent).toBe("[2]");
  });

  it("citation chip exposes the fact text via the `title` attribute (popover fallback)", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "intro",
            heading: "Intro",
            paragraphs: [{ text: "Some prose.", citations: ["f1"] }],
          },
        ])}
        citations={[
          makeCitation("f1", {
            text_excerpt: "OpenClaw runs the Slack bridge.",
            author: "Bob",
            timestamp: "2025-04-01 12:34",
          }),
        ]}
        onNavigate={noop}
      />,
    );
    const chip = screen.getByTestId("narrative-citation-chip");
    const title = chip.getAttribute("title") || "";
    expect(title).toContain("OpenClaw runs the Slack bridge.");
    expect(title).toContain("@Bob");
  });

  it("renders the popover wrapper alongside the chip when the citation is known", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "intro",
            heading: "Intro",
            paragraphs: [{ text: "Some prose.", citations: ["f1"] }],
          },
        ])}
        citations={[makeCitation("f1", { text_excerpt: "Cached fact" })]}
        onNavigate={noop}
      />,
    );
    const popover = screen.getByTestId("narrative-citation-popover");
    expect(popover).toBeInTheDocument();
    expect(popover.textContent || "").toContain("Cached fact");
  });

  it("dedupes display indices across paragraphs (same fact_id → same chip number)", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "s1",
            heading: "S1",
            paragraphs: [
              { text: "First.", citations: ["f-a"] },
              { text: "Second.", citations: ["f-b", "f-a"] },
            ],
          },
        ])}
        citations={[makeCitation("f-a"), makeCitation("f-b")]}
        onNavigate={noop}
      />,
    );
    const chips = screen.getAllByTestId("narrative-citation-chip");
    // Order on the page: f-a (1), f-b (2), f-a (1)
    expect(chips.map((c) => c.textContent)).toEqual(["[1]", "[2]", "[1]"]);
  });
});

// ---------------------------------------------------------------------------
// agent-inference chip
// ---------------------------------------------------------------------------

describe("NarrativeArticleModule — agent-inference paragraphs", () => {
  it("renders an `[agent-inference]` chip when paragraph.is_inference is true", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "intro",
            heading: "Intro",
            paragraphs: [
              {
                text: "These decisions together suggest a shift toward enterprise.",
                citations: ["f1", "f2"],
                is_inference: true,
              },
            ],
          },
        ])}
        citations={[makeCitation("f1"), makeCitation("f2")]}
        onNavigate={noop}
      />,
    );
    const chip = screen.getByTestId("narrative-inference-chip");
    expect(chip).toBeInTheDocument();
    expect(chip.textContent).toBe("[agent-inference]");
  });

  it("does NOT render the inference chip when paragraph.is_inference is false", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "intro",
            heading: "Intro",
            paragraphs: [
              { text: "Direct fact-cite paragraph.", citations: ["f1"], is_inference: false },
            ],
          },
        ])}
        citations={[makeCitation("f1")]}
        onNavigate={noop}
      />,
    );
    expect(screen.queryByTestId("narrative-inference-chip")).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Visual dispatcher
// ---------------------------------------------------------------------------

describe("NarrativeArticleModule — visual dispatcher", () => {
  it("renders a table visual with headers + rows", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "compare",
            heading: "Compare options",
            paragraphs: [{ text: "See below.", citations: ["f1"] }],
            visual: {
              kind: "table",
              content: {
                headers: ["Option", "Cost"],
                rows: [
                  ["JWT", "low"],
                  ["SAML", "high"],
                ],
              },
            },
          },
        ])}
        citations={[makeCitation("f1")]}
        onNavigate={noop}
      />,
    );
    const table = screen.getByTestId("narrative-visual-table");
    expect(table).toBeInTheDocument();
    expect(table.textContent || "").toContain("Option");
    expect(table.textContent || "").toContain("JWT");
    expect(table.textContent || "").toContain("SAML");
  });

  it("renders an unordered list when ordered is false", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "list",
            heading: "Steps",
            paragraphs: [{ text: "Plan.", citations: ["f1"] }],
            visual: {
              kind: "list",
              content: { items: ["First", "Second", "Third"], ordered: false },
            },
          },
        ])}
        citations={[makeCitation("f1")]}
        onNavigate={noop}
      />,
    );
    const list = screen.getByTestId("narrative-visual-list");
    expect(list.tagName.toLowerCase()).toBe("ul");
    expect(list.textContent || "").toContain("First");
    expect(list.textContent || "").toContain("Third");
  });

  it("renders an ordered list when ordered is true", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "list",
            heading: "Steps",
            paragraphs: [{ text: "Plan.", citations: ["f1"] }],
            visual: {
              kind: "list",
              content: { items: ["A", "B"], ordered: true },
            },
          },
        ])}
        citations={[makeCitation("f1")]}
        onNavigate={noop}
      />,
    );
    const list = screen.getByTestId("narrative-visual-list");
    expect(list.tagName.toLowerCase()).toBe("ol");
  });

  it("renders a callout via CalloutBox", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "warn",
            heading: "Warn",
            paragraphs: [{ text: "Take care.", citations: ["f1"] }],
            visual: {
              kind: "callout",
              content: { type: "warning", content: "Mind the gap." },
            },
          },
        ])}
        citations={[makeCitation("f1")]}
        onNavigate={noop}
      />,
    );
    const callout = screen.getByTestId("narrative-visual-callout");
    expect(callout).toBeInTheDocument();
    expect(callout.textContent || "").toContain("Mind the gap.");
  });

  it("renders a code block with language class", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "code",
            heading: "Code",
            paragraphs: [{ text: "See snippet.", citations: ["f1"] }],
            visual: {
              kind: "code",
              content: { language: "python", code: "print('hi')" },
            },
          },
        ])}
        citations={[makeCitation("f1")]}
        onNavigate={noop}
      />,
    );
    const pre = screen.getByTestId("narrative-visual-code");
    expect(pre).toBeInTheDocument();
    const code = pre.querySelector("code");
    expect(code?.className || "").toContain("language-python");
    expect(code?.textContent || "").toContain("print('hi')");
  });

  it("renders a blockquote with attribution", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "quote",
            heading: "Voice",
            paragraphs: [{ text: "Memorable.", citations: ["f1"] }],
            visual: {
              kind: "blockquote",
              content: { content: "Ship small, ship often.", attribution: "Eng team" },
            },
          },
        ])}
        citations={[makeCitation("f1")]}
        onNavigate={noop}
      />,
    );
    const quote = screen.getByTestId("narrative-visual-blockquote");
    expect(quote).toBeInTheDocument();
    expect(quote.textContent || "").toContain("Ship small, ship often.");
    expect(quote.textContent || "").toContain("Eng team");
  });

  it("renders a mermaid visual via the mocked MermaidBlock", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "flow",
            heading: "Flow",
            paragraphs: [{ text: "See diagram.", citations: ["f1"] }],
            visual: { kind: "mermaid", content: "graph TD\nA-->B" },
          },
        ])}
        citations={[makeCitation("f1")]}
        onNavigate={noop}
      />,
    );
    const wrapper = screen.getByTestId("narrative-visual-mermaid");
    expect(wrapper).toBeInTheDocument();
    // Mocked MermaidBlock just echoes the chart string into a stub div.
    expect(screen.getByTestId("mock-mermaid-block").textContent || "").toContain("graph TD");
  });
});

// ---------------------------------------------------------------------------
// Header badges
// ---------------------------------------------------------------------------

describe("NarrativeArticleModule — header badges", () => {
  it("computes reading-time as ceil(words / 200)", () => {
    // 250 words → ceil(250/200) = 2 minutes
    const longText = Array.from({ length: 250 }, (_, i) => `word${i}`).join(" ");
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "intro",
            heading: "Intro",
            paragraphs: [{ text: longText, citations: ["f1"] }],
          },
        ])}
        citations={[makeCitation("f1")]}
        onNavigate={noop}
      />,
    );
    const reading = screen.getByTestId("narrative-reading-time");
    expect(reading.textContent || "").toMatch(/2\s*min read/);
  });

  it("renders 1 min read for short articles (sub-200-word floor)", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "intro",
            heading: "Intro",
            paragraphs: [{ text: "Just a few words here.", citations: ["f1"] }],
          },
        ])}
        citations={[makeCitation("f1")]}
        onNavigate={noop}
      />,
    );
    const reading = screen.getByTestId("narrative-reading-time");
    expect(reading.textContent || "").toMatch(/1\s*min read/);
  });

  it("counts distinct fact_ids for the 'memories synthesized' badge", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "s1",
            heading: "S1",
            paragraphs: [
              { text: "A.", citations: ["f-a", "f-b"] },
              { text: "B.", citations: ["f-a", "f-c"] },
            ],
          },
          {
            anchor: "s2",
            heading: "S2",
            paragraphs: [{ text: "C.", citations: ["f-d"] }],
          },
        ])}
        citations={[
          makeCitation("f-a"),
          makeCitation("f-b"),
          makeCitation("f-c"),
          makeCitation("f-d"),
        ]}
        onNavigate={noop}
      />,
    );
    const badge = screen.getByTestId("narrative-memories-synthesized");
    // 4 distinct fact_ids: f-a, f-b, f-c, f-d
    expect(badge.textContent || "").toMatch(/4\s*memories\s+synthesized/);
  });

  it("uses singular 'memory' label when only one fact is cited", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "s1",
            heading: "S1",
            paragraphs: [{ text: "Lonely.", citations: ["f-only"] }],
          },
        ])}
        citations={[makeCitation("f-only")]}
        onNavigate={noop}
      />,
    );
    const badge = screen.getByTestId("narrative-memories-synthesized");
    expect(badge.textContent || "").toMatch(/1\s*memory\s+synthesized/);
  });
});

// ---------------------------------------------------------------------------
// M-8: anchor sanitisation (defense-in-depth)
// ---------------------------------------------------------------------------

describe("NarrativeArticleModule — anchor sanitisation (M-8)", () => {
  it("renders a valid kebab-case anchor as the section id verbatim", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "context",
            heading: "Context",
            paragraphs: [{ text: "Para.", citations: ["f1"] }],
          },
        ])}
        citations={[makeCitation("f1")]}
        onNavigate={noop}
      />,
    );
    expect(document.getElementById("context")).not.toBeNull();
  });

  it("sanitises HTML-injection anchors so the rendered id is safe", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "</h2><script>alert(1)//",
            heading: "Real heading",
            paragraphs: [{ text: "Para.", citations: ["f1"] }],
          },
        ])}
        citations={[makeCitation("f1")]}
        onNavigate={noop}
      />,
    );
    // The dangerous string must NOT appear as an element id.
    expect(document.getElementById("</h2><script>alert(1)//")).toBeNull();
    // Whatever id the section ends up with must satisfy the kebab-case
    // contract (matches the backend ``_VALID_ANCHOR_RE``).
    const section = screen.getByTestId("narrative-section");
    const heading = section.querySelector("h2");
    const id = heading?.getAttribute("id") || "";
    expect(id).toMatch(/^[a-z0-9][a-z0-9-]{0,23}$/);
  });

  it("uses section-N fallback when anchor + heading slug both fail", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "",
            heading: "—",  // em-dash; slug becomes empty
            paragraphs: [{ text: "Para.", citations: ["f1"] }],
          },
        ])}
        citations={[makeCitation("f1")]}
        onNavigate={noop}
      />,
    );
    const section = screen.getByTestId("narrative-section");
    const heading = section.querySelector("h2");
    expect(heading?.getAttribute("id")).toBe("section-1");
  });
});

// ---------------------------------------------------------------------------
// Inline citation parser — `[f_xxx]` patterns embedded in paragraph text
// ---------------------------------------------------------------------------

describe("NarrativeArticleModule — inline citation parser", () => {
  it("renders a single inline `[f_xxx]` marker as a citation chip in-place", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "intro",
            heading: "Intro",
            paragraphs: [
              {
                text: "Atlas adopted Mattermost as the chat layer [f_4].",
                citations: ["f_4"],
              },
            ],
          },
        ])}
        citations={[makeCitation("f_4")]}
        onNavigate={noop}
      />,
    );
    const chip = screen.getByTestId("narrative-citation-chip");
    expect(chip).toBeInTheDocument();
    expect(chip.getAttribute("data-fact-id")).toBe("f_4");
    expect(chip.textContent).toBe("[1]");
    // The paragraph still shows the prose surrounding the marker.
    const para = screen.getByTestId("narrative-paragraph");
    expect(para.textContent || "").toContain(
      "Atlas adopted Mattermost as the chat layer ",
    );
    expect(para.textContent || "").not.toContain("[f_4]");
  });

  it("expands `[f_xxx, f_yyy]` chains into multiple chips with comma separators", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "intro",
            heading: "Intro",
            paragraphs: [
              {
                text: "Two providers were considered [f_4, f_7].",
                citations: ["f_4", "f_7"],
              },
            ],
          },
        ])}
        citations={[makeCitation("f_4"), makeCitation("f_7")]}
        onNavigate={noop}
      />,
    );
    const chips = screen.getAllByTestId("narrative-citation-chip");
    expect(chips).toHaveLength(2);
    expect(chips[0].getAttribute("data-fact-id")).toBe("f_4");
    expect(chips[1].getAttribute("data-fact-id")).toBe("f_7");
    expect(chips[0].textContent).toBe("[1]");
    expect(chips[1].textContent).toBe("[2]");
    // Comma-separator stays in DOM text between chips.
    const para = screen.getByTestId("narrative-paragraph");
    expect(para.textContent || "").toContain("Two providers were considered ");
    expect(para.textContent || "").not.toMatch(/\[f_4,\s*f_7\]/);
  });

  it("interleaves text segments and chips at the inline-marker positions", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "intro",
            heading: "Intro",
            paragraphs: [
              {
                text: "OpenClaw [f_1] supplanted the legacy bridge [f_2] for Atlas.",
                citations: ["f_1", "f_2"],
              },
            ],
          },
        ])}
        citations={[makeCitation("f_1"), makeCitation("f_2")]}
        onNavigate={noop}
      />,
    );
    const chips = screen.getAllByTestId("narrative-citation-chip");
    expect(chips).toHaveLength(2);
    const para = screen.getByTestId("narrative-paragraph");
    const text = para.textContent || "";
    expect(text).toContain("OpenClaw ");
    expect(text).toContain(" supplanted the legacy bridge ");
    expect(text).toContain(" for Atlas.");
    // The literal `[f_xxx]` patterns must NOT survive into the DOM text.
    expect(text).not.toContain("[f_1]");
    expect(text).not.toContain("[f_2]");
  });

  it("backward compat: paragraph with NO inline markers still renders trailing chips", () => {
    // Old persisted articles authored before the inline-marker prompt
    // change have no ``[f_xxx]`` patterns in ``text`` but DO carry
    // ``paragraph.citations`` — those keep rendering as trailing chips.
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "intro",
            heading: "Intro",
            paragraphs: [
              {
                text: "A pre-inline-marker paragraph with no embedded citations.",
                citations: ["f_a", "f_b"],
              },
            ],
          },
        ])}
        citations={[makeCitation("f_a"), makeCitation("f_b")]}
        onNavigate={noop}
      />,
    );
    const chips = screen.getAllByTestId("narrative-citation-chip");
    expect(chips).toHaveLength(2);
    const para = screen.getByTestId("narrative-paragraph");
    expect(para.textContent || "").toContain(
      "A pre-inline-marker paragraph with no embedded citations.",
    );
  });

  it("does NOT append trailing chips when inline markers were already rendered", () => {
    // When inline markers are present, the renderer must NOT also
    // re-emit trailing chips — that would double-render the citations.
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "intro",
            heading: "Intro",
            paragraphs: [
              {
                text: "Inline [f_a] only.",
                citations: ["f_a"],
              },
            ],
          },
        ])}
        citations={[makeCitation("f_a")]}
        onNavigate={noop}
      />,
    );
    // Exactly one chip — the inline one. No trailing duplicate.
    const chips = screen.getAllByTestId("narrative-citation-chip");
    expect(chips).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// P1: Inline citation chips inside visual content (list/table/callout/blockquote)
// ---------------------------------------------------------------------------
//
// The inline citation parser (`renderParagraphWithInlineCitations`) had been
// applied only to paragraph text. Visual content rendering (list items, table
// cells, callout body, blockquote text) leaked literal `[f_xxx]` patterns.
// These tests pin the new behavior: text inside list/table/callout/blockquote
// runs through the citation parser, while `code` visuals are exempt (code is
// preserved verbatim).

describe("NarrativeArticleModule — citation chips inside visual content (P1)", () => {
  it("renders citation chips inside list items", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "list",
            heading: "Steps",
            paragraphs: [{ text: "See list.", citations: ["f_5", "f_7"] }],
            visual: {
              kind: "list",
              content: {
                items: ["First item [f_5]", "Second item [f_7]"],
                ordered: false,
              },
            },
          },
        ])}
        citations={[makeCitation("f_5"), makeCitation("f_7")]}
        onNavigate={noop}
      />,
    );
    const list = screen.getByTestId("narrative-visual-list");
    // Each list-item carries a citation chip rendered in-place.
    const chips = list.querySelectorAll('[data-testid="narrative-citation-chip"]');
    expect(chips.length).toBe(2);
    expect(chips[0].getAttribute("data-fact-id")).toBe("f_5");
    expect(chips[1].getAttribute("data-fact-id")).toBe("f_7");
    // Literal `[f_xxx]` markers must not survive in the rendered text.
    const text = list.textContent || "";
    expect(text).not.toContain("[f_5]");
    expect(text).not.toContain("[f_7]");
    expect(text).toContain("First item ");
    expect(text).toContain("Second item ");
  });

  it("renders citation chips inside table cells", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "tab",
            heading: "Compare",
            paragraphs: [{ text: "Table.", citations: ["f_3"] }],
            visual: {
              kind: "table",
              content: {
                headers: ["Option", "Notes"],
                rows: [
                  ["JWT", "lower cost [f_3]"],
                  ["SAML", "high effort"],
                ],
              },
            },
          },
        ])}
        citations={[makeCitation("f_3")]}
        onNavigate={noop}
      />,
    );
    const table = screen.getByTestId("narrative-visual-table");
    const chips = table.querySelectorAll('[data-testid="narrative-citation-chip"]');
    expect(chips.length).toBe(1);
    expect(chips[0].getAttribute("data-fact-id")).toBe("f_3");
    expect(table.textContent || "").not.toContain("[f_3]");
  });

  it("renders citation chips inside callout text", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "warn",
            heading: "Warn",
            paragraphs: [{ text: "Callout.", citations: ["f_4"] }],
            visual: {
              kind: "callout",
              content: { variant: "warning", text: "Watch the budget [f_4]." },
            },
          },
        ])}
        citations={[makeCitation("f_4")]}
        onNavigate={noop}
      />,
    );
    const callout = screen.getByTestId("narrative-visual-callout");
    const chip = callout.querySelector('[data-testid="narrative-citation-chip"]');
    expect(chip).not.toBeNull();
    expect(chip?.getAttribute("data-fact-id")).toBe("f_4");
    expect(callout.textContent || "").not.toContain("[f_4]");
    expect(callout.textContent || "").toContain("Watch the budget ");
  });

  it("renders citation chips inside blockquote text", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "quote",
            heading: "Voice",
            paragraphs: [{ text: "Quote.", citations: ["f_2"] }],
            visual: {
              kind: "blockquote",
              content: { content: "Ship it now [f_2].", attribution: "Eng" },
            },
          },
        ])}
        citations={[makeCitation("f_2")]}
        onNavigate={noop}
      />,
    );
    const quote = screen.getByTestId("narrative-visual-blockquote");
    const chip = quote.querySelector('[data-testid="narrative-citation-chip"]');
    expect(chip).not.toBeNull();
    expect(chip?.getAttribute("data-fact-id")).toBe("f_2");
    expect(quote.textContent || "").not.toContain("[f_2]");
  });

  it("does NOT parse citation markers inside code visuals (verbatim contract)", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "code",
            heading: "Snippet",
            paragraphs: [{ text: "See code.", citations: ["f_x"] }],
            visual: {
              kind: "code",
              content: {
                language: "python",
                code: "# refers to [f_x]\nprint('hi')",
              },
            },
          },
        ])}
        citations={[makeCitation("f_x")]}
        onNavigate={noop}
      />,
    );
    const pre = screen.getByTestId("narrative-visual-code");
    // The literal `[f_x]` must remain in the rendered code (verbatim).
    expect(pre.textContent || "").toContain("[f_x]");
    // No chips inside the code block.
    expect(pre.querySelectorAll('[data-testid="narrative-citation-chip"]').length).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// M-3: missing fact_id chip — dev-only [?] fallback (defensive guard)
// ---------------------------------------------------------------------------
//
// The factIdIndex is built from paragraph citations, so under normal flow
// every paragraph fact_id is in the index. The dev-only [?] branch in
// ``ParagraphLine`` is a defensive guard against future code paths that
// supply a pre-built / filtered index (e.g. via shared rendering helpers).
// These tests verify the contract: valid citations render the normal chip
// and never the [?] missing-chip variant.

describe("NarrativeArticleModule — missing fact_id chip (M-3)", () => {
  it("renders normal chip when fact_id is present in the index", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "intro",
            heading: "Intro",
            paragraphs: [{ text: "Some prose.", citations: ["f-known"] }],
          },
        ])}
        citations={[makeCitation("f-known")]}
        onNavigate={noop}
      />,
    );
    const chip = screen.getByTestId("narrative-citation-chip");
    expect(chip).toBeInTheDocument();
    expect(chip.getAttribute("data-fact-id")).toBe("f-known");
    expect(screen.queryByTestId("narrative-citation-chip-missing")).not.toBeInTheDocument();
  });

  it("never renders the [?] missing-chip variant for valid citations", () => {
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "intro",
            heading: "Intro",
            paragraphs: [
              { text: "First.", citations: ["f-a"] },
              { text: "Second.", citations: ["f-b"] },
            ],
          },
        ])}
        citations={[makeCitation("f-a"), makeCitation("f-b")]}
        onNavigate={noop}
      />,
    );
    expect(screen.queryAllByTestId("narrative-citation-chip-missing")).toHaveLength(0);
  });

  it("still renders the normal chip even when the citation has no popover preview", () => {
    // Page-level citations array empty (no WikiCitation popover data),
    // but the paragraph-level fact_id is still in factIdIndex (built
    // from paragraph citations) so the normal chip renders without
    // popover. The [?] branch does NOT trigger here — that is reserved
    // for index-miss only.
    render(
      <NarrativeArticleModule
        module={makeModule([
          {
            anchor: "intro",
            heading: "Intro",
            paragraphs: [{ text: "Some prose.", citations: ["f-orphan"] }],
          },
        ])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    const chip = screen.getByTestId("narrative-citation-chip");
    expect(chip).toBeInTheDocument();
    expect(chip.getAttribute("data-fact-id")).toBe("f-orphan");
    expect(screen.queryByTestId("narrative-citation-chip-missing")).not.toBeInTheDocument();
  });
});
