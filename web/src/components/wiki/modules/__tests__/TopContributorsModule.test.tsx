/**
 * Tests for TopContributorsModule.
 *
 * Coverage:
 *  - Empty items list → null render
 *  - Each chip renders initials, name, contribution count, top page
 *  - Singular vs plural ("contribution" / "contributions")
 *  - Truncation for long page titles
 *  - Initials fall back to first 2 chars when only one name part
 *  - Header renders the "Top contributors" heading
 */

import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { TopContributorsModule } from "../TopContributorsModule";
import type { WikiPageModule } from "@/lib/types";

afterEach(() => cleanup());

interface ContributorFixture {
  name?: string;
  contribution_count?: number;
  top_pages?: Array<{ title?: string; count?: number }>;
}

function makeModule(items: ContributorFixture[] = []): WikiPageModule {
  return {
    id: "top_contributors",
    anchor: "top-contributors",
    data: {
      label: "Top contributors",
      renderer_kind: "frontend",
      items,
    },
  };
}

const noop = () => undefined;

describe("TopContributorsModule", () => {
  it("renders nothing when items list is empty", () => {
    const { container } = render(
      <TopContributorsModule
        module={makeModule([])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders one chip per contributor with name + count + top page", () => {
    render(
      <TopContributorsModule
        module={makeModule([
          {
            name: "Alan Yang",
            contribution_count: 14,
            top_pages: [{ title: "JWT Migration", count: 8 }],
          },
          {
            name: "Bob Smith",
            contribution_count: 7,
            top_pages: [{ title: "OAuth Flow", count: 4 }],
          },
        ])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    const chips = screen.getAllByTestId("top-contributor-chip");
    expect(chips).toHaveLength(2);
    const names = screen
      .getAllByTestId("top-contributor-name")
      .map((n) => n.textContent);
    expect(names).toEqual(["Alan Yang", "Bob Smith"]);
    const counts = screen
      .getAllByTestId("top-contributor-count")
      .map((n) => n.textContent);
    expect(counts).toEqual(["14", "7"]);
    const pages = screen
      .getAllByTestId("top-contributor-top-page")
      .map((n) => n.textContent);
    expect(pages).toEqual(["JWT Migration", "OAuth Flow"]);
  });

  it("renders initials from first + last word of the name", () => {
    render(
      <TopContributorsModule
        module={makeModule([{ name: "Alan Yang", contribution_count: 1 }])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    const initials = screen.getByTestId("top-contributor-initials");
    expect(initials.textContent).toBe("AY");
  });

  it("falls back to first 2 chars when name is a single word", () => {
    render(
      <TopContributorsModule
        module={makeModule([{ name: "alan", contribution_count: 1 }])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    const initials = screen.getByTestId("top-contributor-initials");
    expect(initials.textContent).toBe("AL");
  });

  it("renders the section heading 'Top contributors'", () => {
    render(
      <TopContributorsModule
        module={makeModule([{ name: "Alan", contribution_count: 1 }])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(screen.getByText("Top contributors")).toBeInTheDocument();
  });

  it("truncates long page titles at ~30 chars", () => {
    const longTitle =
      "An Extremely Long Page Title That Definitely Exceeds Thirty Characters";
    render(
      <TopContributorsModule
        module={makeModule([
          {
            name: "Alan",
            contribution_count: 1,
            top_pages: [{ title: longTitle, count: 1 }],
          },
        ])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    const page = screen.getByTestId("top-contributor-top-page");
    expect((page.textContent || "").length).toBeLessThanOrEqual(40);
    expect((page.textContent || "")).toContain("…");
  });

  it("uses singular 'contribution' when count is 1", () => {
    render(
      <TopContributorsModule
        module={makeModule([{ name: "Alan", contribution_count: 1 }])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(screen.getByText("contribution")).toBeInTheDocument();
  });

  it("uses plural 'contributions' when count is > 1", () => {
    render(
      <TopContributorsModule
        module={makeModule([{ name: "Alan", contribution_count: 5 }])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(screen.getByText("contributions")).toBeInTheDocument();
  });

  it("skips chips whose name is blank", () => {
    render(
      <TopContributorsModule
        module={makeModule([
          { name: "", contribution_count: 1 },
          { name: "Alan", contribution_count: 1 },
        ])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(screen.getAllByTestId("top-contributor-chip")).toHaveLength(1);
  });
});
