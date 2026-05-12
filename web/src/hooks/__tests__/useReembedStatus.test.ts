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

const LEGACY_CONFIG = {
  provider: "jina_ai",
  model: "jina-embeddings-v4",
  dimensions: 2048,
  rpm: 60,
  api_base: "",
  task: "",
  has_api_key: true,
  api_key_masked: "ji-...abcd",
  source: "db",
  dim_guard_enabled: true,
  last_probe_at: null,
  last_probe_ok: null,
  last_probe_error: null,
  persisted_provider: "openai",
  persisted_model: "text-embedding-3-large",
  persisted_dimensions: 3072,
  fact_count: 1234,
  migration_required: true,
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
  it("surfaces migration_required + persisted_* from the legacy embedding read", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockImplementation(async (input: any) => {
      const url = String(input);
      if (url.includes("/api/settings/embedding/migrate/status")) return makeResponse(IDLE_STATUS);
      if (url.includes("/api/settings/embedding")) return makeResponse(LEGACY_CONFIG);
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
    // Stop the running poll loop so the test exits cleanly.
    unmount();
  });

  it("the poll loop schedules a follow-up after a transient error and stops on running:false", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.mocked(globalThis.fetch);
    let statusCalls = 0;
    fetchMock.mockImplementation(async (input: any) => {
      const url = String(input);
      if (url.includes("/api/settings/embedding/migrate/status")) {
        statusCalls += 1;
        if (statusCalls === 1) {
          return makeResponse({ ...IDLE_STATUS, running: true, processed: 10, total: 100, stage: "embedding" });
        }
        if (statusCalls === 2) {
          throw new Error("boom"); // transient
        }
        return makeResponse(IDLE_STATUS); // done
      }
      if (url.includes("/api/settings/embedding")) return makeResponse({ ...LEGACY_CONFIG, migration_required: false });
      return makeResponse({});
    });

    const { result, unmount } = renderHook(() => useReembedStatus());

    // Flush the initial poll + the on-mount legacy read.
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

    // No further polls scheduled now that we have an authoritative running:false.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(statusCalls).toBe(3);
    unmount();
  });

  it("startMigration POSTs the migrate endpoint", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    let migratePosted = false;
    fetchMock.mockImplementation(async (input: any, init?: any) => {
      const url = String(input);
      if (url.includes("/api/settings/embedding/migrate") && !url.includes("/status") && init?.method === "POST") {
        migratePosted = true;
        return makeResponse({ job_id: "j1", status: "running" });
      }
      if (url.includes("/api/settings/embedding/migrate/status")) return makeResponse(IDLE_STATUS);
      if (url.includes("/api/settings/embedding")) return makeResponse(LEGACY_CONFIG);
      return makeResponse({});
    });

    const { result, unmount } = renderHook(() => useReembedStatus());
    await waitFor(() => expect(result.current.status).not.toBeNull());

    await act(async () => {
      await result.current.startMigration();
    });
    expect(migratePosted).toBe(true);
    unmount();
  });
});
