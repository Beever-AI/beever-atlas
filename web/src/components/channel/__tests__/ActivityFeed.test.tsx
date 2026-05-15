import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { ActivityFeed } from "../ActivityFeed";
import type { RecentEvent } from "@/lib/types";

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function makeEvents(n: number): RecentEvent[] {
  // Stable timestamps relative to a fixed reference so snapshots are
  // deterministic. Each event 30s older than the previous.
  const base = new Date("2026-05-08T12:00:00.000Z").getTime();
  return Array.from({ length: n }, (_, i) => ({
    ts: new Date(base - i * 30_000).toISOString(),
    stage:
      i % 4 === 0
        ? "extract_facts"
        : i % 4 === 1
          ? "embed"
          : i % 4 === 2
            ? "wiki_maintenance"
            : "persist",
    label: `Event ${i + 1}`,
  }));
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("ActivityFeed — empty state", () => {
  it("renders the empty message when events is null", () => {
    render(<ActivityFeed events={null} />);
    expect(screen.getByTestId("activity-feed-empty")).toHaveTextContent(
      /No activity yet/,
    );
  });

  it("renders the empty message when events is an empty array", () => {
    render(<ActivityFeed events={[]} />);
    expect(screen.getByTestId("activity-feed-empty")).toBeInTheDocument();
  });

  it("uses a custom empty message when provided", () => {
    render(<ActivityFeed events={[]} emptyMessage="Nothing to show yet" />);
    expect(screen.getByTestId("activity-feed-empty")).toHaveTextContent(
      /Nothing to show yet/,
    );
  });
});

describe("ActivityFeed — list rendering", () => {
  it("renders a single event row", () => {
    const events = makeEvents(1);
    render(<ActivityFeed events={events} />);
    expect(screen.getAllByTestId("activity-feed-row")).toHaveLength(1);
    expect(screen.getByText(/Event 1/)).toBeInTheDocument();
  });

  it("renders all 10 events when given 10", () => {
    const events = makeEvents(10);
    render(<ActivityFeed events={events} />);
    expect(screen.getAllByTestId("activity-feed-row")).toHaveLength(10);
  });

  it("caps rendered rows at maxItems when given more than 10", () => {
    const events = makeEvents(15);
    render(<ActivityFeed events={events} />);
    // Default maxItems=10
    expect(screen.getAllByTestId("activity-feed-row")).toHaveLength(10);
  });

  it("respects a custom maxItems prop", () => {
    const events = makeEvents(8);
    render(<ActivityFeed events={events} maxItems={3} />);
    expect(screen.getAllByTestId("activity-feed-row")).toHaveLength(3);
  });
});

describe("ActivityFeed — collapsible mode", () => {
  it("renders expanded by default when collapsible+defaultOpen", () => {
    render(
      <ActivityFeed events={makeEvents(3)} collapsible defaultOpen={true} />,
    );
    expect(screen.getByTestId("activity-feed-list")).toBeInTheDocument();
  });

  it("starts collapsed when defaultOpen=false", () => {
    render(
      <ActivityFeed events={makeEvents(3)} collapsible defaultOpen={false} />,
    );
    expect(screen.queryByTestId("activity-feed-list")).not.toBeInTheDocument();
  });

  it("toggles open on summary click", () => {
    render(
      <ActivityFeed events={makeEvents(3)} collapsible defaultOpen={false} />,
    );
    const button = screen.getByRole("button");
    fireEvent.click(button);
    expect(screen.getByTestId("activity-feed-list")).toBeInTheDocument();
  });
});

describe("ActivityFeed — snapshot", () => {
  it("matches the 5-event snapshot", () => {
    // Freeze relative-time output by stubbing Date.now to a known
    // value so "30s ago" / "1m ago" stays stable across runs.
    const realNow = Date.now;
    Date.now = () => new Date("2026-05-08T12:00:00.000Z").getTime();
    try {
      const { container } = render(<ActivityFeed events={makeEvents(5)} />);
      expect(container.firstChild).toMatchSnapshot();
    } finally {
      Date.now = realNow;
    }
  });
});
