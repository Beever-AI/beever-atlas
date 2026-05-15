/**
 * Tests for the Key Facts module v2 — frontend card list renderer.
 *
 * Coverage:
 *  (a) critical fact promotion (flat strip outside any group)
 *  (b) grouping by fact_type with humanized labels
 *  (c) collapse/expand interactions ("Show N more" + row expansion)
 *  (d) URL hyperlinking inside title and body text
 *  (e) author chip dedup (≥80% same author → single header chip)
 *
 * Privacy: all fixtures use synthetic data (Acme / placeholder names,
 * example.com URLs).
 */

import { describe, it, expect, afterEach } from "vitest";
import {
  render,
  screen,
  cleanup,
  fireEvent,
  within,
} from "@testing-library/react";
import { KeyFactsModule } from "../KeyFactsModule";
import type { WikiPageModule } from "@/lib/types";

afterEach(() => cleanup());

interface FactFixture {
  fact_id?: string;
  title: string;
  body?: string;
  fact_type?: string;
  importance?: string;
  author_name?: string;
  ts?: string;
  source_url?: string;
}

function makeItem(fixture: FactFixture) {
  return {
    fact_id: fixture.fact_id || `fact-${fixture.title.slice(0, 8)}`,
    title: fixture.title,
    body: fixture.body ?? fixture.title,
    fact_type: fixture.fact_type || "observation",
    importance: fixture.importance || "medium",
    author: { name: fixture.author_name || "", id: "" },
    ts: fixture.ts || "",
    source: { url: fixture.source_url || "", platform: "" },
    citations: [],
  };
}

function makeModule(items: ReturnType<typeof makeItem>[]): WikiPageModule {
  return {
    id: "key_facts",
    anchor: "key-facts",
    data: {
      label: "Key Facts",
      renderer_kind: "frontend",
      items,
      groups: [
        "decision",
        "observation",
        "open_question",
        "action_item",
        "opinion",
      ],
    },
  };
}

const noop = () => undefined;

// ---------------------------------------------------------------------------
// (a) Critical fact promotion
// ---------------------------------------------------------------------------

describe("KeyFactsModule — critical promotion", () => {
  it("promotes critical facts to the top strip outside any group", () => {
    const items = [
      makeItem({
        title: "Auth service exposed PII via debug endpoint.",
        importance: "critical",
        fact_type: "observation",
      }),
      makeItem({
        title: "Adopt JWT for service auth.",
        importance: "high",
        fact_type: "decision",
      }),
    ];
    render(
      <KeyFactsModule
        module={makeModule(items)}
        citations={[]}
        onNavigate={noop}
      />,
    );
    const strip = screen.getByTestId("key-facts-critical-strip");
    expect(strip).toBeInTheDocument();
    expect(
      within(strip).getByText(/Auth service exposed PII/),
    ).toBeInTheDocument();
    // Critical fact does NOT appear inside the Decisions/Observations group.
    const observationsGroup = screen.queryByTestId(
      "key-facts-group-observation",
    );
    if (observationsGroup) {
      expect(
        within(observationsGroup).queryByText(/Auth service exposed PII/),
      ).not.toBeInTheDocument();
    }
  });

  it("renders no critical strip when no critical facts exist", () => {
    const items = [
      makeItem({ title: "Plain fact.", importance: "medium" }),
    ];
    render(
      <KeyFactsModule
        module={makeModule(items)}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(
      screen.queryByTestId("key-facts-critical-strip"),
    ).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// (b) Grouping with humanized labels
// ---------------------------------------------------------------------------

describe("KeyFactsModule — grouping", () => {
  it("groups facts by fact_type with humanized labels", () => {
    const items = [
      makeItem({ title: "Decision A.", fact_type: "decision" }),
      makeItem({ title: "Observation B.", fact_type: "observation" }),
      makeItem({
        title: "How to rotate?",
        fact_type: "open_question",
      }),
      makeItem({
        title: "Ship the migration.",
        fact_type: "action_item",
      }),
    ];
    render(
      <KeyFactsModule
        module={makeModule(items)}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(screen.getByText("Decisions")).toBeInTheDocument();
    expect(screen.getByText("Observations")).toBeInTheDocument();
    expect(screen.getByText("Open Questions")).toBeInTheDocument();
    expect(screen.getByText("Action Items")).toBeInTheDocument();
  });

  it("renders nothing when items array is empty", () => {
    const { container } = render(
      <KeyFactsModule
        module={makeModule([])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(container.firstChild).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// (c) Collapse / expand
// ---------------------------------------------------------------------------

describe("KeyFactsModule — collapse / expand", () => {
  it("shows first 3 facts in a group with a 'Show N more' expander", () => {
    const items = Array.from({ length: 5 }, (_, i) =>
      makeItem({
        fact_id: `fact-${i}`,
        title: `Observation number ${i}.`,
        fact_type: "observation",
      }),
    );
    render(
      <KeyFactsModule
        module={makeModule(items)}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(screen.getByText(/Show 2 more/)).toBeInTheDocument();
    // First 3 visible; last 2 hidden until expansion.
    expect(screen.getByText(/Observation number 0./)).toBeInTheDocument();
    expect(screen.getByText(/Observation number 2./)).toBeInTheDocument();
    expect(
      screen.queryByText(/Observation number 4./),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByText(/Show 2 more/));
    expect(screen.getByText(/Observation number 4./)).toBeInTheDocument();
  });

  it("expands a row to reveal full body + source link on click", () => {
    const items = [
      makeItem({
        title: "Short title.",
        body: "Short title. The full body text is much longer.",
        source_url: "https://example.com/source",
      }),
    ];
    render(
      <KeyFactsModule
        module={makeModule(items)}
        citations={[]}
        onNavigate={noop}
      />,
    );
    // Body collapsed initially.
    expect(
      screen.queryByText(/full body text/),
    ).not.toBeInTheDocument();
    // Click the row to expand.
    fireEvent.click(screen.getByText(/Short title./));
    expect(screen.getByText(/full body text/)).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /source/ });
    expect(link).toHaveAttribute("href", "https://example.com/source");
  });
});

// ---------------------------------------------------------------------------
// (d) URL hyperlinking
// ---------------------------------------------------------------------------

describe("KeyFactsModule — URL hyperlinking", () => {
  it("hyperlinks URLs found in fact title text", () => {
    const items = [
      makeItem({
        title: "See https://example.com/rfc for details.",
      }),
    ];
    render(
      <KeyFactsModule
        module={makeModule(items)}
        citations={[]}
        onNavigate={noop}
      />,
    );
    const link = screen.getByRole("link", {
      name: /https:\/\/example.com\/rfc/,
    });
    expect(link).toHaveAttribute("href", "https://example.com/rfc");
    expect(link).toHaveAttribute("target", "_blank");
  });

  it("hyperlinks URLs found in fact body text on expansion", () => {
    const items = [
      makeItem({
        title: "Plain title.",
        body: "Body refers to https://example.org/doc-2 for context.",
      }),
    ];
    render(
      <KeyFactsModule
        module={makeModule(items)}
        citations={[]}
        onNavigate={noop}
      />,
    );
    fireEvent.click(screen.getByText(/Plain title./));
    const link = screen.getByRole("link", {
      name: /https:\/\/example.org\/doc-2/,
    });
    expect(link).toHaveAttribute("href", "https://example.org/doc-2");
  });
});

// ---------------------------------------------------------------------------
// (e) SeverityBadge integration — every fact row must render a
//     SeverityBadge (color + icon + aria-label) instead of the prior
//     color-only dot, so color-blind readers can still tell severity
//     buckets apart.
// ---------------------------------------------------------------------------

describe("KeyFactsModule — SeverityBadge a11y", () => {
  it("renders a SeverityBadge with aria-label per fact row", () => {
    const items = [
      makeItem({
        title: "Critical fact.",
        importance: "critical",
        fact_type: "observation",
      }),
      makeItem({
        title: "High fact.",
        importance: "high",
        fact_type: "decision",
      }),
      makeItem({
        title: "Medium fact.",
        importance: "medium",
        fact_type: "observation",
      }),
      makeItem({
        title: "Low fact.",
        importance: "low",
        fact_type: "observation",
      }),
    ];
    render(
      <KeyFactsModule
        module={makeModule(items)}
        citations={[]}
        onNavigate={noop}
      />,
    );
    const badges = screen.getAllByTestId("severity-badge");
    // 4 facts → 4 badges.
    expect(badges.length).toBe(4);
    const labels = badges.map((b) => b.getAttribute("aria-label"));
    expect(labels).toContain("Critical importance");
    expect(labels).toContain("High importance");
    expect(labels).toContain("Medium importance");
    expect(labels).toContain("Low importance");
  });
});

// ---------------------------------------------------------------------------
// (f) Glossary tooltips — when a glossary is provided in module data,
//     matching whole words inside fact titles get wrapped in a span
//     with title="<definition>" + dotted-underline class.
// ---------------------------------------------------------------------------

describe("KeyFactsModule — glossary tooltips", () => {
  it("wraps glossary terms in a title-tooltip span", () => {
    const items = [
      makeItem({
        title: "Adopt MFA for the SSO flow.",
        fact_type: "decision",
      }),
    ];
    const mod = makeModule(items);
    // Inject a glossary into module data — the renderer reads it
    // off ``module.data.glossary``.
    (mod.data as { glossary?: Record<string, string> }).glossary = {
      MFA: "Multi-Factor Authentication",
      SSO: "Single Sign-On",
    };
    render(
      <KeyFactsModule
        module={mod}
        citations={[]}
        onNavigate={noop}
      />,
    );
    // Both terms become highlighted spans with the matching title.
    const mfa = document.querySelector(
      '[data-glossary-term="MFA"]',
    ) as HTMLElement | null;
    expect(mfa).not.toBeNull();
    expect(mfa?.getAttribute("title")).toBe("Multi-Factor Authentication");
    const sso = document.querySelector(
      '[data-glossary-term="SSO"]',
    ) as HTMLElement | null;
    expect(sso).not.toBeNull();
    expect(sso?.getAttribute("title")).toBe("Single Sign-On");
  });

  it("ignores partial-word matches (whole-word boundaries only)", () => {
    const items = [
      // "MFAuthenticator" should NOT highlight "MFA" — it's not a
      // word boundary match.
      makeItem({
        title: "MFAuthenticator was rejected.",
        fact_type: "decision",
      }),
    ];
    const mod = makeModule(items);
    (mod.data as { glossary?: Record<string, string> }).glossary = {
      MFA: "Multi-Factor Authentication",
    };
    render(
      <KeyFactsModule
        module={mod}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(
      document.querySelector('[data-glossary-term="MFA"]'),
    ).toBeNull();
  });

  it("renders no tooltips when no glossary is provided", () => {
    const items = [
      makeItem({
        title: "Adopt MFA for SSO.",
        fact_type: "decision",
      }),
    ];
    render(
      <KeyFactsModule
        module={makeModule(items)}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(
      document.querySelector("[data-glossary-term]"),
    ).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// (g) Author chip dedup
// ---------------------------------------------------------------------------

describe("KeyFactsModule — author chip dedup", () => {
  it("shows a single 'by Author (× N)' header when ≥80% same author", () => {
    const items = [
      makeItem({
        title: "Decision 1.",
        fact_type: "decision",
        author_name: "Alice",
      }),
      makeItem({
        title: "Decision 2.",
        fact_type: "decision",
        author_name: "Alice",
      }),
      makeItem({
        title: "Decision 3.",
        fact_type: "decision",
        author_name: "Alice",
      }),
      makeItem({
        title: "Decision 4.",
        fact_type: "decision",
        author_name: "Alice",
      }),
      makeItem({
        title: "Decision 5.",
        fact_type: "decision",
        author_name: "Bob",
      }),
    ];
    render(
      <KeyFactsModule
        module={makeModule(items)}
        citations={[]}
        onNavigate={noop}
      />,
    );
    // Alice = 4/5 = 80% → header shows the dedup chip.
    expect(screen.getByText(/by Alice/)).toBeInTheDocument();
    expect(screen.getByText(/× 4/)).toBeInTheDocument();
  });

  it("renders per-row author chip when authors are mixed (<80% share)", () => {
    const items = [
      makeItem({
        title: "Decision 1.",
        fact_type: "decision",
        author_name: "Alice",
      }),
      makeItem({
        title: "Decision 2.",
        fact_type: "decision",
        author_name: "Bob",
      }),
      makeItem({
        title: "Decision 3.",
        fact_type: "decision",
        author_name: "Charlie",
      }),
    ];
    render(
      <KeyFactsModule
        module={makeModule(items)}
        citations={[]}
        onNavigate={noop}
      />,
    );
    // No dedup header — each row shows its author.
    expect(screen.queryByText(/by Alice/)).not.toBeInTheDocument();
    expect(screen.getByText(/Alice/)).toBeInTheDocument();
    expect(screen.getByText(/Bob/)).toBeInTheDocument();
    expect(screen.getByText(/Charlie/)).toBeInTheDocument();
  });
});
