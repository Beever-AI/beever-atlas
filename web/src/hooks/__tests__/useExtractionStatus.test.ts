import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useExtractionStatus } from "../useExtractionStatus";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeFetchResponse(body: unknown, ok = true, status = 200): Response {
  return {
    ok,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

const MOCK_STATUS = {
  channel_id: "ch-1",
  counts: { pending: 10, extracting: 3, done: 87, failed: 0 },
  total: 100,
};

const MOCK_STATUS_IDLE = {
  channel_id: "ch-1",
  counts: { pending: 0, extracting: 0, done: 100, failed: 0 },
  total: 100,
};

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("useExtractionStatus", () => {
  it("fetches status on mount", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockResolvedValue(makeFetchResponse(MOCK_STATUS));

    const { result } = renderHook(() =>
      useExtractionStatus("ch-1"),
    );

    await act(async () => {
      await Promise.resolve();
    });

    expect(result.current.status).toEqual(MOCK_STATUS);
    expect(result.current.error).toBeNull();
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("sets error when fetch fails", async () => {
    vi.mocked(globalThis.fetch).mockRejectedValueOnce(new TypeError("Network error"));

    const { result } = renderHook(() => useExtractionStatus("ch-1"));

    await act(async () => {
      await Promise.resolve();
    });

    expect(result.current.error).toBeTruthy();
    expect(result.current.status).toBeNull();
  });

  it("polls at idle cadence (30s default) when isSyncing=false", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockResolvedValue(makeFetchResponse(MOCK_STATUS_IDLE));

    renderHook(() =>
      useExtractionStatus("ch-1", { isSyncing: false, pollMsIdle: 30000 }),
    );

    // Initial fetch
    await act(async () => { await Promise.resolve(); });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Advance 29s — no additional fetch
    await act(async () => { vi.advanceTimersByTime(29000); });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Advance 1 more second to reach 30s — one more fetch
    await act(async () => {
      vi.advanceTimersByTime(1000);
      await Promise.resolve();
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("polls at active cadence (5s default) when isSyncing=true", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockResolvedValue(makeFetchResponse(MOCK_STATUS));

    renderHook(() =>
      useExtractionStatus("ch-1", { isSyncing: true, pollMsActive: 5000 }),
    );

    // Initial fetch
    await act(async () => { await Promise.resolve(); });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // After 5s, expect a second fetch
    await act(async () => {
      vi.advanceTimersByTime(5000);
      await Promise.resolve();
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("cadence flips from idle to active when isSyncing changes", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockResolvedValue(makeFetchResponse(MOCK_STATUS));

    const { rerender } = renderHook(
      ({ isSyncing }: { isSyncing: boolean }) =>
        useExtractionStatus("ch-1", {
          isSyncing,
          pollMsActive: 5000,
          pollMsIdle: 30000,
        }),
      { initialProps: { isSyncing: false } },
    );

    // Initial fetch at mount
    await act(async () => { await Promise.resolve(); });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Switch to syncing — interval should flip to 5s
    rerender({ isSyncing: true });

    // Advance 5s; should have another fetch
    await act(async () => {
      vi.advanceTimersByTime(5000);
      await Promise.resolve();
    });
    // At least 2 calls total (initial + one 5s poll)
    expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it("does nothing when channelId is null", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);

    const { result } = renderHook(() => useExtractionStatus(null));

    await act(async () => { await Promise.resolve(); });

    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.current.status).toBeNull();
  });

  it("stops polling when unmounted", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockResolvedValue(makeFetchResponse(MOCK_STATUS));

    const { unmount } = renderHook(() =>
      useExtractionStatus("ch-1", { pollMsIdle: 5000 }),
    );

    await act(async () => { await Promise.resolve(); });
    const callsAtUnmount = fetchMock.mock.calls.length;

    unmount();

    await act(async () => {
      vi.advanceTimersByTime(10000);
      await Promise.resolve();
    });

    expect(fetchMock.mock.calls.length).toBe(callsAtUnmount);
  });
});
