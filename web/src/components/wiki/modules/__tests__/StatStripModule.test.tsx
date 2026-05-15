/**
 * Tests for the StatStripModule.
 *
 * Coverage:
 *  - Empty stats → null render (no zero-card strip)
 *  - Each card renders value (large) + label (small uppercase)
 *  - Period label renders below the strip when both dates set
 *  - Period label hides when no dates supplied
 *  - Cards skip when value is empty (defensive against bad data)
 */

import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { StatStripModule } from "../StatStripModule";
import type { WikiPageModule } from "@/lib/types";

afterEach(() => cleanup());

interface StatFixture {
  value?: string;
  label?: string;
  fact_id?: string;
  raw_value?: number | null;
}

function makeModule(
  stats: StatFixture[] = [],
  period: { from?: string; to?: string } = {},
): WikiPageModule {
  return {
    id: "stat_strip",
    anchor: "stat-strip",
    data: {
      label: "Stats",
      renderer_kind: "frontend",
      stats,
      period,
    },
  };
}

const noop = () => undefined;

describe("StatStripModule", () => {
  it("renders nothing when stats list is empty", () => {
    const { container } = render(
      <StatStripModule
        module={makeModule([])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders one card per stat with value + label", () => {
    render(
      <StatStripModule
        module={makeModule([
          { value: "2,396", label: "actions" },
          { value: "534k", label: "impressions" },
          { value: "HK$130k", label: "paid-media equivalent" },
        ])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    const cards = screen.getAllByTestId("stat-strip-card");
    expect(cards).toHaveLength(3);
    const values = screen.getAllByTestId("stat-strip-value").map((n) => n.textContent);
    expect(values).toEqual(["2,396", "534k", "HK$130k"]);
    const labels = screen.getAllByTestId("stat-strip-label").map((n) => n.textContent);
    expect(labels).toEqual(["actions", "impressions", "paid-media equivalent"]);
  });

  it("renders the period label when both from and to are set", () => {
    render(
      <StatStripModule
        module={makeModule(
          [{ value: "2,396", label: "actions" }],
          { from: "2026-04-26", to: "2026-05-02" },
        )}
        citations={[]}
        onNavigate={noop}
      />,
    );
    const period = screen.getByTestId("stat-strip-period");
    expect(period.textContent || "").toMatch(/Apr 26.*May 2.*2026/);
  });

  it("hides the period label when no dates are supplied", () => {
    render(
      <StatStripModule
        module={makeModule([{ value: "2,396", label: "actions" }], {})}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(screen.queryByTestId("stat-strip-period")).not.toBeInTheDocument();
  });

  it("skips cards whose value is empty", () => {
    render(
      <StatStripModule
        module={makeModule([
          { value: "", label: "ghost" },
          { value: "534k", label: "impressions" },
        ])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    const cards = screen.getAllByTestId("stat-strip-card");
    // The empty-value entry returns null inside .map, so only the
    // real card renders.
    expect(cards).toHaveLength(1);
  });

  it("renders the value with a large display size class", () => {
    render(
      <StatStripModule
        module={makeModule([{ value: "534k", label: "impressions" }])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    const value = screen.getByTestId("stat-strip-value");
    // Large display size + tabular-nums for digit alignment.
    expect(value.className).toMatch(/text-2xl/);
    expect(value.className).toMatch(/font-semibold/);
    expect(value.className).toMatch(/tabular-nums/);
  });
});
