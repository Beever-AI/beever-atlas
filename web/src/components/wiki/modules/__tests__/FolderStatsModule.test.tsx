/**
 * Tests for FolderStatsModule — folder index dashboard's 4-card strip.
 *
 * Coverage:
 *  - Empty stats list → null render
 *  - Each card renders value + label + folder icon
 *  - Renders four cards in canonical order
 *  - Cards skip when both value and label are blank
 *  - Tabular-nums + large-display class on value
 */

import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { FolderStatsModule } from "../FolderStatsModule";
import type { WikiPageModule } from "@/lib/types";

afterEach(() => cleanup());

interface StatFixture {
  value?: string;
  label?: string;
}

function makeModule(stats: StatFixture[] = [], subpageCount = 0): WikiPageModule {
  return {
    id: "folder_stats",
    anchor: "folder-stats",
    data: {
      label: "Folder stats",
      renderer_kind: "frontend",
      stats,
      subpage_count: subpageCount,
    },
  };
}

const noop = () => undefined;

describe("FolderStatsModule", () => {
  it("renders nothing when stats list is empty", () => {
    const { container } = render(
      <FolderStatsModule
        module={makeModule([])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders all four canonical cards in order", () => {
    render(
      <FolderStatsModule
        module={makeModule([
          { value: "30", label: "memories" },
          { value: "2", label: "decisions" },
          { value: "0", label: "open questions" },
          { value: "4", label: "contributors" },
        ])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    const cards = screen.getAllByTestId("folder-stat-card");
    expect(cards).toHaveLength(4);
    const labels = screen
      .getAllByTestId("folder-stat-label")
      .map((n) => n.textContent);
    expect(labels).toEqual([
      "memories",
      "decisions",
      "open questions",
      "contributors",
    ]);
    const values = screen
      .getAllByTestId("folder-stat-value")
      .map((n) => n.textContent);
    expect(values).toEqual(["30", "2", "0", "4"]);
  });

  it("renders the value with a large display size class + tabular-nums", () => {
    render(
      <FolderStatsModule
        module={makeModule([{ value: "30", label: "memories" }])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    const value = screen.getByTestId("folder-stat-value");
    expect(value.className).toMatch(/text-2xl/);
    expect(value.className).toMatch(/tabular-nums/);
    expect(value.className).toMatch(/font-semibold/);
  });

  it("skips cards whose value AND label are blank", () => {
    render(
      <FolderStatsModule
        module={makeModule([
          { value: "", label: "" },
          { value: "30", label: "memories" },
        ])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    const cards = screen.getAllByTestId("folder-stat-card");
    expect(cards).toHaveLength(1);
  });
});
