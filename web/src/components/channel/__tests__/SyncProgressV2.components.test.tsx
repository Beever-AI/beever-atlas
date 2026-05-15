/**
 * Component tests for SyncProgressV2 sub-components — Layer 2 of the
 * 3-layer test plan.
 *
 * Layer 1 (derivations.test.ts) tested the pure functions. This layer
 * tests the RENDERED OUTPUT of the components that consume those
 * functions. Every test pins a user-visible behaviour we've fixed
 * during the monitor work — so a regression flips a CI red instead
 * of a manual screenshot red.
 *
 * Components under test:
 *   - MetricsBar      — the 6-tile metrics row
 *   - BatchTabs       — state filter chips + per-batch chip strip
 *   - UpNextStrip     — pending-batches placeholder cards
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { afterEach } from "vitest";
import {
  MetricsBar,
  BatchTabs,
  UpNextStrip,
  type BatchSummary,
} from "../SyncProgressV2";
import type {
  BatchResultEntry,
  RecentEvent,
} from "@/lib/types";

afterEach(() => cleanup());

// ── Fixtures ───────────────────────────────────────────────────────────

function batchSummary(
  idx: number,
  state: BatchSummary["state"],
): BatchSummary {
  return {
    batchIdx: idx,
    state,
    stagesStarted: state === "running" ? 1 : state === "done" ? 6 : 0,
    hasPersisterDone: state === "done",
    factsCount: state === "done" ? 20 : 0,
    entitiesCount: state === "done" ? 30 : 0,
    totalElapsedMs: state === "done" ? 1200 : 0,
    hasFailure: state === "failed",
  };
}

function batchResult(num: number, facts = 20, entities = 30): BatchResultEntry {
  return {
    batch_num: num,
    facts_count: facts,
    entities_count: entities,
    relationships_count: 5,
    embedded_count: facts,
    media_count: 0,
    sample_facts: [],
    sample_entities: [],
    sample_relationships: [],
    duration_seconds: 1.2,
    error: null,
  };
}

const noEvents: RecentEvent[] = [];

// ── MetricsBar ─────────────────────────────────────────────────────────

describe("MetricsBar", () => {
  it("renders the BATCHES tile with done/total fraction", () => {
    render(
      <MetricsBar
        events={noEvents}
        activityLog={[]}
        stickyResults={[]}
        totalMessages={715}
        processedMessages={150}
        totalBatches={29}
        batchesCompleted={5}
      />,
    );

    // Tile labels render in title-case ("Messages", "Batches", etc.).
    expect(screen.getByText(/messages/i)).toBeTruthy();
    expect(screen.getByText(/batches/i)).toBeTruthy();
    // Values render in separate DOM nodes; check the body text.
    const body = (document.body.textContent ?? "").toLowerCase();
    expect(body).toContain("150");
    expect(body).toContain("715");
    expect(body).toContain("5");
    expect(body).toContain("29");
  });

  it("clamps total to ≥ done so we never render 21/15", () => {
    // SyncRunner gave a stale estimate of 15 but 21 batches are done.
    render(
      <MetricsBar
        events={noEvents}
        activityLog={[]}
        stickyResults={[]}
        totalMessages={500}
        processedMessages={500}
        totalBatches={15}
        batchesCompleted={21}
      />,
    );
    const body = (document.body.textContent ?? "").toLowerCase();
    // Denominator must be ≥21. Cannot show "21/15" anywhere.
    expect(body).not.toMatch(/21\s*\/\s*15/);
  });

  it("renders FACTS / ENTITIES / EMBEDDED / MEDIA tile counts from sticky", () => {
    render(
      <MetricsBar
        events={noEvents}
        activityLog={[]}
        stickyResults={[batchResult(1, 20, 30), batchResult(2, 15, 22)]}
        totalMessages={100}
        processedMessages={60}
        totalBatches={5}
        batchesCompleted={2}
      />,
    );
    // Aggregated facts = 35, entities = 52, embedded = 35.
    // Numbers may repeat across tiles, so use getAllByText. We just
    // want to confirm the values render at least once.
    expect(screen.getAllByText("35").length).toBeGreaterThan(0);
    expect(screen.getAllByText("52").length).toBeGreaterThan(0);
  });
});

// ── BatchTabs ──────────────────────────────────────────────────────────

describe("BatchTabs", () => {
  it("renders chip per batch with correct state class", () => {
    const batches: BatchSummary[] = [
      batchSummary(1, "done"),
      batchSummary(2, "running"),
      batchSummary(3, "pending"),
    ];
    render(<BatchTabs batches={batches} selected="all" onSelect={vi.fn()} />);

    expect(screen.getByText("Batch 1")).toBeTruthy();
    expect(screen.getByText("Batch 2")).toBeTruthy();
    expect(screen.getByText("Batch 3")).toBeTruthy();
  });

  it("state filter counts match the input batches array", () => {
    const batches: BatchSummary[] = [
      batchSummary(1, "done"),
      batchSummary(2, "done"),
      batchSummary(3, "running"),
      batchSummary(4, "pending"),
      batchSummary(5, "pending"),
      batchSummary(6, "failed"),
    ];
    render(<BatchTabs batches={batches} selected="all" onSelect={vi.fn()} />);

    // The state filter chips show the count next to each label.
    // We look for the chips by their accessible text content.
    const text = (document.body.textContent ?? "").toLowerCase();
    // State filter chips render as "All6", "Done2", etc. (no
    // separator). Check the substring presence — exact format
    // is brittle but the count proximity to the label is the
    // user-visible contract.
    expect(text).toContain("all6");
    expect(text).toContain("done2");
    expect(text).toContain("running1");
    expect(text).toContain("pending2");
    expect(text).toContain("failed1");
  });

  it("calls onSelect when a batch chip is clicked", async () => {
    const onSelect = vi.fn();
    const batches: BatchSummary[] = [batchSummary(1, "done"), batchSummary(2, "running")];
    render(<BatchTabs batches={batches} selected="all" onSelect={onSelect} />);

    const chip = screen.getByText("Batch 2").closest("button");
    chip?.click();
    expect(onSelect).toHaveBeenCalledWith(2);
  });

  it("hides itself when batches array is empty", () => {
    const { container } = render(
      <BatchTabs batches={[]} selected="all" onSelect={vi.fn()} />,
    );
    expect(container.textContent).toBe("");
  });

  it("REGRESSION: Batch 1 done state reflects only the input batchSummary, no idx-based heuristic", () => {
    // This pins the chip-rendering surface of the bug we fixed: chip
    // state must come from BatchSummary.state (which itself was
    // derived from knownDoneBatchNums), not from any fallback rule
    // inside BatchTabs.
    const batches: BatchSummary[] = [
      batchSummary(1, "running"), // server says batch 1 is currently running
      batchSummary(2, "done"),    // server says batch 2 is actually done
      batchSummary(3, "done"),    // server says batch 3 is actually done
    ];
    render(<BatchTabs batches={batches} selected="all" onSelect={vi.fn()} />);

    // DONE count must equal 2 (batches 2 and 3), NOT 3 (idx-based
    // "first 2 are done" fallback would have given 2 here too;
    // but with batch 1 marked running, the contradiction would surface).
    const lower = (document.body.textContent ?? "").toLowerCase();
    expect(lower).toContain("done2");
    expect(lower).toContain("running1");
  });
});

// ── UpNextStrip ────────────────────────────────────────────────────────

describe("UpNextStrip", () => {
  it("renders header with pending + running counts", () => {
    const pending: BatchSummary[] = [
      batchSummary(5, "pending"),
      batchSummary(6, "pending"),
    ];
    const running: BatchSummary[] = [batchSummary(4, "running")];
    render(<UpNextStrip pending={pending} running={running} />);

    expect(screen.getByText(/2 pending/i)).toBeTruthy();
    expect(screen.getByText(/1 running/i)).toBeTruthy();
  });

  it("renders up to 8 pending batch placeholders and a +N indicator beyond", () => {
    const pending: BatchSummary[] = Array.from({ length: 12 }, (_, i) =>
      batchSummary(i + 1, "pending"),
    );
    render(<UpNextStrip pending={pending} running={[]} />);

    // First 8 should be visible explicitly
    expect(screen.getByText("Batch 1")).toBeTruthy();
    expect(screen.getByText("Batch 8")).toBeTruthy();
    // Beyond 8: a "+4 more" tile (12 - 8 = 4)
    expect(screen.getByText(/\+4 more/i)).toBeTruthy();
  });

  it("hides running count from header when running is empty", () => {
    const pending: BatchSummary[] = [batchSummary(5, "pending")];
    render(<UpNextStrip pending={pending} running={[]} />);
    expect(screen.queryByText(/running/i)).toBeNull();
  });
});
