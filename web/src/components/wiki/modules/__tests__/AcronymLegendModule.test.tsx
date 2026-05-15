/**
 * Tests for the AcronymLegendModule.
 *
 * Coverage:
 *  - Empty items → null render (no orphan legend section)
 *  - Each row renders the term + truncated definition
 *  - Long definitions truncate to ~120 chars at a word boundary
 *  - Term column gets the mono / accent styling testid hooks
 */

import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { AcronymLegendModule } from "../AcronymLegendModule";
import type { WikiPageModule } from "@/lib/types";

afterEach(() => cleanup());

interface LegendItemFixture {
  term?: string;
  definition?: string;
  first_mentioned_by?: string;
}

function makeModule(items: LegendItemFixture[] = []): WikiPageModule {
  return {
    id: "acronym_legend",
    anchor: "acronym",
    data: {
      label: "Terms used on this page",
      renderer_kind: "frontend",
      items,
    },
  };
}

const noop = () => undefined;

describe("AcronymLegendModule", () => {
  it("renders nothing when items list is empty", () => {
    const { container } = render(
      <AcronymLegendModule
        module={makeModule([])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders the section heading + each row", () => {
    render(
      <AcronymLegendModule
        module={makeModule([
          { term: "MFA", definition: "Multi-Factor Authentication" },
          { term: "OIDC", definition: "OpenID Connect" },
          { term: "SAML", definition: "Security Assertion Markup Language" },
        ])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(screen.getByText(/Terms used on this page/)).toBeInTheDocument();
    const rows = screen.getAllByTestId("acronym-legend-row");
    expect(rows).toHaveLength(3);
  });

  it("renders the term in mono / accent styling", () => {
    render(
      <AcronymLegendModule
        module={makeModule([{ term: "MFA", definition: "Multi-Factor Authentication" }])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    const term = screen.getByTestId("acronym-legend-term");
    expect(term).toHaveTextContent("MFA");
    // Mono styling lives in the className — assert the rendering hook
    // for accent + mono is present.
    expect(term.className).toMatch(/font-mono/);
    expect(term.className).toMatch(/text-blue-600|text-blue-400/);
  });

  it("truncates a long definition to ~120 chars + ellipsis", () => {
    const longDef =
      "An identity protocol layered on top of OAuth 2.0 that lets clients verify the identity of an end user based on the authentication performed by an authorization server.";
    render(
      <AcronymLegendModule
        module={makeModule([{ term: "OIDC", definition: longDef }])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    const def = screen.getByTestId("acronym-legend-definition");
    expect(def.textContent || "").toContain("…");
    // Allow up to 121 chars (120 budget + ellipsis).
    expect((def.textContent || "").length).toBeLessThanOrEqual(121);
  });

  it("leaves a short definition intact", () => {
    render(
      <AcronymLegendModule
        module={makeModule([{ term: "MFA", definition: "Multi-Factor Authentication" }])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    const def = screen.getByTestId("acronym-legend-definition");
    expect(def).toHaveTextContent("Multi-Factor Authentication");
    expect(def.textContent || "").not.toContain("…");
  });

  it("skips rows with empty term", () => {
    render(
      <AcronymLegendModule
        module={makeModule([
          { term: "", definition: "ghost" },
          { term: "MFA", definition: "Multi-Factor Authentication" },
        ])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    const rows = screen.getAllByTestId("acronym-legend-row");
    // The empty-term row was a hole that didn't render its testid
    // wrapper — only the real row renders.
    expect(rows).toHaveLength(1);
    expect(screen.getByTestId("acronym-legend-term")).toHaveTextContent("MFA");
  });
});
