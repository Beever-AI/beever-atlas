import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { PhasedProgressCard } from "../PhasedProgressCard";
import type { Phase } from "@/lib/types";

// ---------------------------------------------------------------------------
// Fixtures — one per scenario from spec U1 acceptance criteria.
// ---------------------------------------------------------------------------

const MID_EXTRACTION: Phase[] = [
  { name: "fetched", state: "done", done: 700, total: 700, duration_ms: 1800 },
  {
    name: "extracting",
    state: "in_flight",
    done: 230,
    total: 700,
    last_event_label: "Extracting facts from 25 rows",
  },
  { name: "wiki_maintenance", state: "pending" },
  { name: "overview_wiki", state: "pending" },
];

const ALL_DONE: Phase[] = [
  { name: "fetched", state: "done", done: 700, total: 700, duration_ms: 1800 },
  { name: "extracting", state: "done", done: 700, total: 700, duration_ms: 240_000 },
  { name: "wiki_maintenance", state: "done", done: 12, total: 12, duration_ms: 60_000 },
  { name: "overview_wiki", state: "done", duration_ms: 28_000 },
];

const SKIPPED: Phase[] = [
  { name: "fetched", state: "done", done: 5, total: 5 },
  { name: "extracting", state: "done", done: 5, total: 5 },
  { name: "wiki_maintenance", state: "skipped" },
  { name: "overview_wiki", state: "skipped" },
];

const MIXED_RETRYING: Phase[] = [
  { name: "fetched", state: "done", done: 200, total: 200 },
  {
    name: "extracting",
    state: "in_flight",
    done: 150,
    total: 200,
    last_event_label: "Retrying after 429",
  },
  { name: "wiki_maintenance", state: "pending" },
  { name: "overview_wiki", state: "pending" },
];

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("PhasedProgressCard — visual states", () => {
  it("renders the four phase rows in order", () => {
    render(<PhasedProgressCard phases={MID_EXTRACTION} smoothedEtaSeconds={120} />);
    expect(screen.getByTestId("phase-row-fetched")).toBeInTheDocument();
    expect(screen.getByTestId("phase-row-extracting")).toBeInTheDocument();
    expect(screen.getByTestId("phase-row-wiki_maintenance")).toBeInTheDocument();
    expect(screen.getByTestId("phase-row-overview_wiki")).toBeInTheDocument();
  });

  it("shows the smoothed ETA next to the active phase when in flight", () => {
    render(<PhasedProgressCard phases={MID_EXTRACTION} smoothedEtaSeconds={120} />);
    const card = screen.getByTestId("phased-progress-card");
    expect(card).toHaveTextContent(/2 min remaining/);
  });

  it("renders 'Calculating…' when ETA is null and a phase is in flight", () => {
    render(<PhasedProgressCard phases={MID_EXTRACTION} smoothedEtaSeconds={null} />);
    expect(screen.getByTestId("phased-progress-card")).toHaveTextContent(
      /Calculating/,
    );
  });

  it("matches the mid-extraction snapshot", () => {
    const { container } = render(
      <PhasedProgressCard
        phases={MID_EXTRACTION}
        smoothedEtaSeconds={120}
        retrying={0}
        abandoned={0}
      />,
    );
    expect(container.firstChild).toMatchSnapshot();
  });

  it("matches the all-done snapshot", () => {
    const { container } = render(
      <PhasedProgressCard phases={ALL_DONE} smoothedEtaSeconds={null} />,
    );
    expect(container.firstChild).toMatchSnapshot();
  });

  it("matches the skipped snapshot", () => {
    const { container } = render(
      <PhasedProgressCard phases={SKIPPED} smoothedEtaSeconds={null} />,
    );
    expect(container.firstChild).toMatchSnapshot();
  });

  it("matches the mixed-retrying snapshot with chips visible", () => {
    const { container } = render(
      <PhasedProgressCard
        phases={MIXED_RETRYING}
        smoothedEtaSeconds={45}
        retrying={3}
        abandoned={2}
        channelId="C1"
        onOpenFailedDrillDown={vi.fn()}
      />,
    );
    expect(container.firstChild).toMatchSnapshot();
  });
});

describe("PhasedProgressCard — retrying / abandoned chips", () => {
  it("renders a retrying chip when ``retrying > 0``", () => {
    render(
      <PhasedProgressCard
        phases={MIXED_RETRYING}
        smoothedEtaSeconds={null}
        retrying={3}
      />,
    );
    const chip = screen.getByTestId("retrying-chip");
    expect(chip).toHaveTextContent(/Retrying/);
    expect(chip).toHaveTextContent(/3 rows/);
  });

  it("hides chips when both counts are zero", () => {
    render(
      <PhasedProgressCard
        phases={MIXED_RETRYING}
        smoothedEtaSeconds={null}
        retrying={0}
        abandoned={0}
      />,
    );
    expect(screen.queryByTestId("retrying-chip")).not.toBeInTheDocument();
    expect(screen.queryByTestId("abandoned-chip")).not.toBeInTheDocument();
  });

  it("fires the drill-down callback when the abandoned chip is clicked", () => {
    const handler = vi.fn();
    render(
      <PhasedProgressCard
        phases={MIXED_RETRYING}
        smoothedEtaSeconds={null}
        abandoned={2}
        channelId="C1"
        onOpenFailedDrillDown={handler}
      />,
    );
    const chip = screen.getByTestId("abandoned-chip");
    fireEvent.click(chip);
    expect(handler).toHaveBeenCalledWith("C1");
  });

  it("singular vs plural copy adapts to count", () => {
    render(
      <PhasedProgressCard
        phases={MIXED_RETRYING}
        smoothedEtaSeconds={null}
        retrying={1}
        abandoned={1}
        channelId="C1"
        onOpenFailedDrillDown={vi.fn()}
      />,
    );
    expect(screen.getByTestId("retrying-chip")).toHaveTextContent(/1 row\b/);
    expect(screen.getByTestId("abandoned-chip")).toHaveTextContent(/1 row\b/);
  });
});

describe("PhasedProgressCard — phase ordering", () => {
  it("synthesizes pending rows for missing phases (graceful degradation)", () => {
    // Backend sends only the first two — UI should still render all 4.
    const partial: Phase[] = [
      { name: "fetched", state: "done", done: 5, total: 5 },
      { name: "extracting", state: "in_flight", done: 2, total: 5 },
    ];
    render(<PhasedProgressCard phases={partial} smoothedEtaSeconds={null} />);
    expect(screen.getByTestId("phase-row-wiki_maintenance")).toBeInTheDocument();
    expect(screen.getByTestId("phase-row-overview_wiki")).toBeInTheDocument();
  });
});
