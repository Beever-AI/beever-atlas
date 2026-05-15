import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";
import { useReembedStatus } from "../useReembedStatus";

function makeResponse(body: unknown, ok = true, status = 200): Response {
  return {
    ok,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

const IDLE_STATUS = {
  running: false,
  job_id: null,
  stage: null,
  processed: null,
  total: null,
  started_at: null,
  finished_at: null,
  error: null,
};

const STATE_NEEDS_REEMBED = {
  migration_required: true,
  desired_provider: "jina_ai",
  desired_model: "jina-embeddings-v4",
  desired_dimensions: 2048,
  persisted_provider: "openai",
  persisted_model: "text-embedding-3-large",
  persisted_dimensions: 3072,
  fact_count: 1234,
  reembed_supported: true,
  reason: null,
};

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("useReembedStatus", () => {
  it("surfaces migration_required + persisted_* from GET /embedding-migration/state", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockImplementation(async (input: any) => {
      const url = String(input);
      if (url.includes("/api/settings/embedding-migration/status")) return makeResponse(IDLE_STATUS);
      if (url.includes("/api/settings/embedding-migration/state")) return makeResponse(STATE_NEEDS_REEMBED);
      return makeResponse({});
    });

    const { result, unmount } = renderHook(() => useReembedStatus());

    await waitFor(() => expect(result.current.migrationRequired).toBe(true));
    expect(result.current.persisted).toEqual({
      provider: "openai",
      model: "text-embedding-3-large",
      dim: 3072,
      count: 1234,
    });
    expect(result.current.reembedSupported).toBe(true);
    expect(result.current.reembedSupportReason).toBeNull();
    // Stop the running poll loop so the test exits cleanly.
    unmount();
  });

  it("surfaces reembed_supported=false + the reason for an unsupported endpoint", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockImplementation(async (input: any) => {
      const url = String(input);
      if (url.includes("/api/settings/embedding-migration/status")) return makeResponse(IDLE_STATUS);
      if (url.includes("/api/settings/embedding-migration/state"))
        return makeResponse({
          ...STATE_NEEDS_REEMBED,
          migration_required: false,
          desired_provider: "anthropic",
          reembed_supported: false,
          reason: "endpoint preset 'anthropic' isn't a direct embedding provider — re-embed not yet supported via proxy endpoints",
        });
      return makeResponse({});
    });

    const { result, unmount } = renderHook(() => useReembedStatus());
    await waitFor(() => expect(result.current.reembedSupported).toBe(false));
    expect(result.current.reembedSupportReason).toMatch(/anthropic/);
    unmount();
  });

  it("the poll loop schedules a follow-up after a transient error and stops on running:false", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.mocked(globalThis.fetch);
    let statusCalls = 0;
    fetchMock.mockImplementation(async (input: any) => {
      const url = String(input);
      if (url.includes("/api/settings/embedding-migration/status")) {
        statusCalls += 1;
        if (statusCalls === 1) {
          return makeResponse({ ...IDLE_STATUS, running: true, processed: 10, total: 100, stage: "embedding" });
        }
        if (statusCalls === 2) {
          throw new Error("boom"); // transient
        }
        return makeResponse(IDLE_STATUS); // done
      }
      if (url.includes("/api/settings/embedding-migration/state"))
        return makeResponse({ ...STATE_NEEDS_REEMBED, migration_required: false });
      return makeResponse({});
    });

    const { result, unmount } = renderHook(() => useReembedStatus());

    // Flush the initial poll + the on-mount state read.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(statusCalls).toBe(1);
    expect(result.current.status?.running).toBe(true);

    // The next poll is scheduled 2s out — it throws (transient).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(statusCalls).toBe(2);

    // After the transient error the loop backs off 4s and polls again — done.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000);
    });
    expect(statusCalls).toBe(3);
    expect(result.current.status?.running).toBe(false);
    expect(result.current.isPolling).toBe(false);

    // PR-θ: the poll loop now keeps running at the 8s idle cadence even
    // when running=false. Otherwise the first running:false response (which
    // commonly happens on mount, before any spawn) would freeze the loop
    // and a subsequent user-triggered re-embed would never surface in the UI.
    // After +10s past the running:false response (next idle poll at +8s),
    // we should see exactly one more poll.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(statusCalls).toBe(4);
    unmount();
  });

  it("startMigration POSTs the /embedding-migration/spawn endpoint", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    let spawnPosted = false;
    fetchMock.mockImplementation(async (input: any, init?: any) => {
      const url = String(input);
      if (url.includes("/api/settings/embedding-migration/spawn") && init?.method === "POST") {
        spawnPosted = true;
        return makeResponse({ job_id: "j1", status: "running" });
      }
      if (url.includes("/api/settings/embedding-migration/status")) return makeResponse(IDLE_STATUS);
      if (url.includes("/api/settings/embedding-migration/state")) return makeResponse(STATE_NEEDS_REEMBED);
      return makeResponse({});
    });

    const { result, unmount } = renderHook(() => useReembedStatus());
    await waitFor(() => expect(result.current.status).not.toBeNull());

    await act(async () => {
      await result.current.startMigration();
    });
    expect(spawnPosted).toBe(true);
    unmount();
  });
});
