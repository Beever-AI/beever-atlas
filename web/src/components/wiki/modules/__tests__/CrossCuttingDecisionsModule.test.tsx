/**
 * Tests for CrossCuttingDecisionsModule.
 *
 * Coverage:
 *  - Empty items list → null render
 *  - Each item renders title, importance badge, author, date, source link
 *  - Severity color classes (critical=red, high=amber, medium=primary, low=muted)
 *  - Source-page link calls onNavigate with `topic-<slug>`
 *  - Date formatting (YYYY-MM-DD → "Mon DD, YYYY")
 *  - Items skip when title is blank
 */

import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { CrossCuttingDecisionsModule } from "../CrossCuttingDecisionsModule";
import type { WikiPageModule } from "@/lib/types";

afterEach(() => cleanup());

interface DecisionFixture {
  fact_id?: string;
  title?: string;
  decided_by?: string;
  decided_at?: string;
  importance?: string;
  source_page?: { title?: string; slug?: string };
}

function makeModule(items: DecisionFixture[] = []): WikiPageModule {
  return {
    id: "cross_cutting_decisions",
    anchor: "cross-decisions",
    data: {
      label: "Cross-cutting decisions",
      renderer_kind: "frontend",
      items,
    },
  };
}

const noop = () => undefined;

describe("CrossCuttingDecisionsModule", () => {
  it("renders nothing when items list is empty", () => {
    const { container } = render(
      <CrossCuttingDecisionsModule
        module={makeModule([])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders one item per decision with title + importance + author + date", () => {
    render(
      <CrossCuttingDecisionsModule
        module={makeModule([
          {
            fact_id: "f1",
            title: "Adopt JWT for session auth.",
            decided_by: "Alan Yang",
            decided_at: "2026-04-15",
            importance: "high",
            source_page: { title: "JWT Migration", slug: "jwt-migration" },
          },
          {
            fact_id: "f2",
            title: "Deprecate SAML by Q3.",
            decided_by: "Bob Smith",
            decided_at: "2026-04-20",
            importance: "critical",
            source_page: { title: "Auth Roadmap", slug: "auth-roadmap" },
          },
        ])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    const items = screen.getAllByTestId("cross-cutting-decision-item");
    expect(items).toHaveLength(2);
    const titles = screen
      .getAllByTestId("cross-cutting-decision-title")
      .map((n) => n.textContent);
    expect(titles).toEqual([
      "Adopt JWT for session auth.",
      "Deprecate SAML by Q3.",
    ]);
    const importances = screen
      .getAllByTestId("cross-cutting-decision-importance")
      .map((n) => n.textContent);
    expect(importances).toEqual(["high", "critical"]);
  });

  it("formats ISO dates as 'Mon DD, YYYY'", () => {
    render(
      <CrossCuttingDecisionsModule
        module={makeModule([
          {
            title: "Adopt JWT.",
            decided_at: "2026-04-15",
            importance: "high",
            source_page: { title: "X", slug: "x" },
          },
        ])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    const date = screen.getByTestId("cross-cutting-decision-date");
    expect(date.textContent).toBe("Apr 15, 2026");
  });

  it("calls onNavigate with topic-<slug> when the source link is clicked", () => {
    const onNavigate = vi.fn();
    render(
      <CrossCuttingDecisionsModule
        module={makeModule([
          {
            title: "Adopt JWT.",
            importance: "high",
            source_page: { title: "JWT Migration", slug: "jwt-migration" },
          },
        ])}
        citations={[]}
        onNavigate={onNavigate}
      />,
    );
    const link = screen.getByTestId("cross-cutting-decision-source-link");
    fireEvent.click(link);
    expect(onNavigate).toHaveBeenCalledWith("topic-jwt-migration");
  });

  it("renders severity-coloured left border for critical importance", () => {
    render(
      <CrossCuttingDecisionsModule
        module={makeModule([
          {
            title: "Critical decision.",
            importance: "critical",
            source_page: { title: "X", slug: "x" },
          },
        ])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    const item = screen.getByTestId("cross-cutting-decision-item");
    expect(item.className).toMatch(/border-l-red-500/);
  });

  it("renders severity-coloured left border for high importance", () => {
    render(
      <CrossCuttingDecisionsModule
        module={makeModule([
          {
            title: "High decision.",
            importance: "high",
            source_page: { title: "X", slug: "x" },
          },
        ])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    const item = screen.getByTestId("cross-cutting-decision-item");
    expect(item.className).toMatch(/border-l-amber-500/);
  });

  it("renders the section heading 'Cross-cutting decisions'", () => {
    render(
      <CrossCuttingDecisionsModule
        module={makeModule([
          {
            title: "Adopt JWT.",
            importance: "high",
            source_page: { title: "X", slug: "x" },
          },
        ])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(screen.getByText("Cross-cutting decisions")).toBeInTheDocument();
  });

  it("skips items whose title is blank", () => {
    render(
      <CrossCuttingDecisionsModule
        module={makeModule([
          { title: "", importance: "high" },
          {
            title: "Real decision.",
            importance: "high",
            source_page: { title: "X", slug: "x" },
          },
        ])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(screen.getAllByTestId("cross-cutting-decision-item")).toHaveLength(1);
  });

  it("hides the source link when slug is missing", () => {
    render(
      <CrossCuttingDecisionsModule
        module={makeModule([
          {
            title: "Adopt JWT.",
            importance: "high",
            source_page: { title: "X" },
          },
        ])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(
      screen.queryByTestId("cross-cutting-decision-source-link"),
    ).not.toBeInTheDocument();
  });
});
