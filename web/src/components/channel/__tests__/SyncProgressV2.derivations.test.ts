/**
 * Unit tests for the pure derivation functions in SyncProgressV2.
 *
 * Every test here corresponds to a real UI bug we shipped a fix for
 * during the May 2026 sync-monitor work. The tests pin the post-fix
 * behaviour so regressions surface in CI instead of via manual
 * screenshots.
 *
 * Functions under test:
 *   - summariseBatches           — per-batch state derivation
 *   - deriveBatchResultsFromActivity — fallback batch-results
 *   - deriveMetrics              — MetricsBar tile totals
 */

import { describe, it, expect } from "vitest";
import {
  summariseBatches,
  deriveBatchResultsFromActivity,
  deriveMetrics,
} from "../SyncProgressV2";
import type {
  ActivityEntry,
  BatchResultEntry,
  RecentEvent,
} from "@/lib/types";

// ── Fixtures ───────────────────────────────────────────────────────────

function persisterEvent(batchIdx: number): ActivityEntry {
  return {
    batch_idx: batchIdx,
    type: "stage_output",
    agent: "persister",
    stage: "Step 6/6 — Saving to stores",
    message: `Saved 20 facts → Weaviate, 25 entities → Neo4j`,
    elapsed: 1.2,
  } as ActivityEntry;
}

function stageStart(batchIdx: number, agent = "preprocessor"): ActivityEntry {
  return {
    batch_idx: batchIdx,
    type: "stage_start",
    agent,
    stage: "Step 1/6 — Preprocessing messages",
  } as ActivityEntry;
}

function factExtractorOutput(batchIdx: number, count: number): ActivityEntry {
  return {
    batch_idx: batchIdx,
    type: "stage_output",
    agent: "fact_extractor",
    stage: "Step 2/6 — Extracting facts (LLM)",
    metrics: { count } as Record<string, unknown>,
  } as ActivityEntry;
}

function batchResult(num: number, facts = 20, entities = 30): BatchResultEntry {
  return {
    batch_num: num,
    facts_count: facts,
    entities_count: entities,
    relationships_count: 0,
    embedded_count: facts,
    media_count: 0,
    sample_facts: [],
    sample_entities: [],
    sample_relationships: [],
    duration_seconds: 1.2,
    error: null,
  };
}

// ── summariseBatches ───────────────────────────────────────────────────

describe("summariseBatches", () => {
  it("out-of-order completion: Batch 5 done before Batch 1, knownDoneBatchNums wins", () => {
    // Only Batch 2's persister event survived the $slice eviction.
    // Sticky results contain {2, 5} as known-done.
    const activityLog: ActivityEntry[] = [persisterEvent(2)];
    const knownDone = new Set([2, 5]);

    const result = summariseBatches(activityLog, 29, 2, knownDone);

    // CRITICAL — Batch 1 must NOT be marked done just because
    // batchesCompleted=2. Out-of-order completion means {2, 5} are
    // the real done batches.
    expect(result.find((b) => b.batchIdx === 1)?.state).toBe("pending");
    expect(result.find((b) => b.batchIdx === 2)?.state).toBe("done");
    expect(result.find((b) => b.batchIdx === 5)?.state).toBe("done");
    // All other batches should stay pending.
    expect(result.find((b) => b.batchIdx === 3)?.state).toBe("pending");
    expect(result.find((b) => b.batchIdx === 10)?.state).toBe("pending");
  });

  it("legacy fallback: when knownDoneBatchNums is undefined, fall back to idx ≤ done", () => {
    const result = summariseBatches([], 5, 3, undefined);
    expect(result.find((b) => b.batchIdx === 1)?.state).toBe("done");
    expect(result.find((b) => b.batchIdx === 3)?.state).toBe("done");
    expect(result.find((b) => b.batchIdx === 4)?.state).toBe("pending");
  });

  it("partial events + sticky-known-done: still marks done correctly", () => {
    // Batch 1 has stage_start but persister event was evicted from
    // the activity_log slice. Without the knownDoneBatchNums override,
    // it would render as "running" forever.
    const activityLog: ActivityEntry[] = [
      stageStart(1, "preprocessor"),
      stageStart(1, "fact_extractor"),
    ];
    const knownDone = new Set([1]);

    const result = summariseBatches(activityLog, 5, 1, knownDone);

    expect(result.find((b) => b.batchIdx === 1)?.state).toBe("done");
  });

  it("active running: stage_start without persister and not in knownDoneBatchNums", () => {
    const activityLog: ActivityEntry[] = [stageStart(3)];
    const result = summariseBatches(activityLog, 10, 0, new Set());
    expect(result.find((b) => b.batchIdx === 3)?.state).toBe("running");
  });

  it("empty activity_log + empty knownDoneBatchNums = all pending", () => {
    const result = summariseBatches([], 5, 0, new Set());
    expect(result.every((b) => b.state === "pending")).toBe(true);
    expect(result).toHaveLength(5);
  });

  it("persister event in activity_log auto-marks done (no knownDone needed)", () => {
    const activityLog: ActivityEntry[] = [
      stageStart(1, "preprocessor"),
      persisterEvent(1),
    ];
    const result = summariseBatches(activityLog, 3, 1, new Set());
    expect(result.find((b) => b.batchIdx === 1)?.state).toBe("done");
  });

  it("returns batches sorted by batchIdx ascending", () => {
    const activityLog: ActivityEntry[] = [
      persisterEvent(3),
      persisterEvent(1),
    ];
    const result = summariseBatches(activityLog, 5, 2, new Set([1, 3]));
    expect(result.map((b) => b.batchIdx)).toEqual([1, 2, 3, 4, 5]);
  });

  it("totalBatches=0 returns only batches with activity_log entries", () => {
    const activityLog: ActivityEntry[] = [persisterEvent(7)];
    const result = summariseBatches(activityLog, 0, 0, new Set());
    expect(result.map((b) => b.batchIdx)).toEqual([7]);
    expect(result[0].state).toBe("done");
  });
});

// ── deriveBatchResultsFromActivity ─────────────────────────────────────

describe("deriveBatchResultsFromActivity", () => {
  it("collects facts_count from fact_extractor events per batch", () => {
    const activityLog: ActivityEntry[] = [
      factExtractorOutput(1, 15),
      factExtractorOutput(2, 22),
      factExtractorOutput(1, 5), // additional event for batch 1
    ];
    const result = deriveBatchResultsFromActivity(activityLog);
    const b1 = result.find((r) => r.batch_num === 1);
    const b2 = result.find((r) => r.batch_num === 2);
    expect(b1?.facts_count).toBe(20); // 15 + 5
    expect(b2?.facts_count).toBe(22);
  });

  it("returns empty array when no batch_idx is set", () => {
    const activityLog: ActivityEntry[] = [
      { type: "stage_start", agent: "preprocessor", batch_idx: null } as unknown as ActivityEntry,
    ];
    const result = deriveBatchResultsFromActivity(activityLog);
    expect(result).toHaveLength(0);
  });
});

// ── deriveMetrics ──────────────────────────────────────────────────────

describe("deriveMetrics", () => {
  const noEvents: RecentEvent[] = [];

  it("total batches clamps to at least batchesCompleted (never N/N-1)", () => {
    // SyncRunner gave us a stale estimate of 15 but worker has already
    // completed 21. The denominator must show ≥21 so we never render
    // the user-confusing "21/15".
    const metrics = deriveMetrics(noEvents, [], [], 15, 21);
    expect(metrics.totalBatches).toBeGreaterThanOrEqual(21);
    expect(metrics.batchesDone).toBe(21);
  });

  it("sums facts/entities/embedded/media from sticky_results", () => {
    const sticky: BatchResultEntry[] = [
      batchResult(1, 20, 30),
      batchResult(2, 15, 22),
    ];
    const metrics = deriveMetrics(noEvents, [], sticky, 5, 2);
    expect(metrics.totalFacts).toBe(35);
    expect(metrics.totalEntities).toBe(52);
    expect(metrics.totalEmbedded).toBe(35);
  });

  it("batchesInFlight counts batches with stage_start minus persister-completed", () => {
    const activityLog: ActivityEntry[] = [
      stageStart(1),
      stageStart(2),
      stageStart(3),
      persisterEvent(1), // batch 1 done
    ];
    const metrics = deriveMetrics(noEvents, activityLog, [], 5, 1);
    expect(metrics.batchesInFlight).toBe(2); // batches 2, 3 still in flight
  });

  it("empty inputs: all zero, total clamps to jobTotalBatches if given", () => {
    const metrics = deriveMetrics(noEvents, [], [], 10, 0);
    expect(metrics.totalBatches).toBe(10);
    expect(metrics.batchesDone).toBe(0);
    expect(metrics.totalFacts).toBe(0);
    expect(metrics.batchesInFlight).toBe(0);
  });
});
