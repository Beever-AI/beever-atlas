/**
 * useDeleteChannel hook tests.
 *
 * Guards: correct URL construction, success side-effects (release +
 * connections-changed), 207 partial warning path, error path.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useDeleteChannel } from "../useDeleteChannel";

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const mockRelease = vi.fn();
vi.mock("@/contexts/SyncStatusContext", () => ({
  useSyncStatus: () => ({
    syncingChannels: new Set<string>(),
    claim: vi.fn(),
    release: mockRelease,
  }),
}));

const mockShow = vi.fn();
vi.mock("@/hooks/useToast", () => ({
  useToast: () => ({
    toasts: [],
    show: mockShow,
    dismiss: vi.fn(),
  }),
}));

// ---------------------------------------------------------------------------
// fetch helpers
// ---------------------------------------------------------------------------

function makeResponse(body: unknown, ok: boolean, status: number): Response {
  return {
    ok,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("useDeleteChannel", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("calls DELETE with correct URL including confirm param", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockResolvedValue(
      makeResponse(
        { channel_id: "ch-1", status: "completed", counts: {}, errors: {} },
        true,
        200,
      ),
    );

    const { result } = renderHook(() => useDeleteChannel());

    await act(async () => {
      await result.current.remove("ch-1", "general");
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/channels/ch-1");
    expect(String(url)).toContain("confirm=general");
    expect(init?.method).toBe("DELETE");
  });

  it("encodes special characters in channelName confirm param", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockResolvedValue(
      makeResponse(
        { channel_id: "ch-2", status: "completed", counts: {}, errors: {} },
        true,
        200,
      ),
    );

    const { result } = renderHook(() => useDeleteChannel());

    await act(async () => {
      await result.current.remove("ch-2", "my channel & stuff");
    });

    const [url] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("confirm=my%20channel%20%26%20stuff");
  });

  it("on 200 completed: calls release + dispatches connections-changed + success toast", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockResolvedValue(
      makeResponse(
        { channel_id: "ch-1", status: "completed", counts: {}, errors: {} },
        true,
        200,
      ),
    );

    const dispatchedEvents: string[] = [];
    window.addEventListener("connections-changed", () =>
      dispatchedEvents.push("connections-changed"),
    );

    const { result } = renderHook(() => useDeleteChannel());

    await act(async () => {
      await result.current.remove("ch-1", "general");
    });

    expect(mockRelease).toHaveBeenCalledWith("ch-1");
    expect(dispatchedEvents).toContain("connections-changed");
    expect(mockShow).toHaveBeenCalledWith(expect.stringContaining("deleted"), "info");

    window.removeEventListener("connections-changed", () => {});
  });

  it("on 200 already_in_progress: no release, no connections-changed, info toast", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockResolvedValue(
      makeResponse(
        {
          channel_id: "ch-1",
          status: "already_in_progress",
          message: "Already running",
        },
        true,
        200,
      ),
    );

    const dispatchedEvents: string[] = [];
    const handler = () => dispatchedEvents.push("connections-changed");
    window.addEventListener("connections-changed", handler);

    const { result } = renderHook(() => useDeleteChannel());

    await act(async () => {
      await result.current.remove("ch-1", "general");
    });

    expect(mockRelease).not.toHaveBeenCalled();
    expect(dispatchedEvents).not.toContain("connections-changed");
    expect(mockShow).toHaveBeenCalledWith(expect.stringContaining("Already running"), "info");

    window.removeEventListener("connections-changed", handler);
  });

  it("on 207 partial: calls release + dispatches connections-changed + warning toast", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    // Response.ok is true for all 2xx including 207, so api.delete returns
    // the body normally. The hook branches on result.status === "partial".
    fetchMock.mockResolvedValue(
      makeResponse(
        {
          channel_id: "ch-1",
          status: "partial",
          counts: {},
          errors: { weaviate: "timeout" },
        },
        true, // ok=true for 207
        207,
      ),
    );

    const dispatchedEvents: string[] = [];
    const handler = () => dispatchedEvents.push("connections-changed");
    window.addEventListener("connections-changed", handler);

    const { result } = renderHook(() => useDeleteChannel());

    let returnedResult: Awaited<ReturnType<typeof result.current.remove>> | undefined;
    await act(async () => {
      returnedResult = await result.current.remove("ch-1", "general");
    });

    // Side-effects SHOULD fire for 207 (channel is mostly gone, reaper converges)
    expect(mockRelease).toHaveBeenCalledWith("ch-1");
    expect(dispatchedEvents).toContain("connections-changed");
    // Informational toast ("info" variant) — partial mostly succeeded and the
    // reaper self-heals, so it is not surfaced as an error.
    expect(mockShow).toHaveBeenCalledWith(
      expect.stringContaining("partially deleted"),
      "info",
    );
    // Returns a result object (doesn't re-throw)
    expect(returnedResult).toBeDefined();
    expect(returnedResult?.status).toBe("partial");

    window.removeEventListener("connections-changed", handler);
  });

  it("on non-207 error (500): throws, no release, no connections-changed", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockResolvedValue(
      makeResponse({ detail: "Internal Server Error" }, false, 500),
    );

    const dispatchedEvents: string[] = [];
    const handler = () => dispatchedEvents.push("connections-changed");
    window.addEventListener("connections-changed", handler);

    const { result } = renderHook(() => useDeleteChannel());

    await act(async () => {
      await expect(result.current.remove("ch-1", "general")).rejects.toThrow();
    });

    expect(mockRelease).not.toHaveBeenCalled();
    expect(dispatchedEvents).not.toContain("connections-changed");

    window.removeEventListener("connections-changed", handler);
  });

  it("on 404: treats as already-deleted — resolves completed, releases, fires connections-changed", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockResolvedValue(
      makeResponse({ detail: "Channel not found" }, false, 404),
    );

    const dispatchedEvents: string[] = [];
    const handler = () => dispatchedEvents.push("connections-changed");
    window.addEventListener("connections-changed", handler);

    const { result } = renderHook(() => useDeleteChannel());

    let returnedResult:
      | Awaited<ReturnType<typeof result.current.remove>>
      | undefined;
    await act(async () => {
      returnedResult = await result.current.remove("ch-1", "general");
    });

    // 404 = already gone: idempotent success, not a thrown error.
    expect(returnedResult?.status).toBe("completed");
    expect(returnedResult?.channel_id).toBe("ch-1");
    expect(mockRelease).toHaveBeenCalledWith("ch-1");
    expect(dispatchedEvents).toContain("connections-changed");
    expect(mockShow).toHaveBeenCalledWith(
      expect.stringContaining("already deleted"),
      "info",
    );

    window.removeEventListener("connections-changed", handler);
  });

  it("loading state is true while request is in flight and false after", async () => {
    let resolveRequest!: (v: Response) => void;
    const pendingPromise = new Promise<Response>((res) => {
      resolveRequest = res;
    });

    vi.mocked(globalThis.fetch).mockReturnValue(pendingPromise);

    const { result } = renderHook(() => useDeleteChannel());
    expect(result.current.loading).toBe(false);

    let removePromise!: Promise<unknown>;
    act(() => {
      removePromise = result.current.remove("ch-1", "general");
    });

    await waitFor(() => expect(result.current.loading).toBe(true));

    act(() => {
      resolveRequest(
        makeResponse(
          { channel_id: "ch-1", status: "completed", counts: {}, errors: {} },
          true,
          200,
        ),
      );
    });

    await act(async () => {
      await removePromise;
    });

    expect(result.current.loading).toBe(false);
  });
});
