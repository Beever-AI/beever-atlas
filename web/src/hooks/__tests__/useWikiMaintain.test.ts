import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useWikiMaintain } from "../useWikiMaintain";

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

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("useWikiMaintain", () => {
  it("starts with loading=false, result=null, error=null", () => {
    const { result } = renderHook(() => useWikiMaintain("ch-1"));
    expect(result.current.loading).toBe(false);
    expect(result.current.result).toBeNull();
    expect(result.current.error).toBeNull();
  });

  it("transitions to loading=true while maintain is in flight", async () => {
    let resolveReq!: (v: Response) => void;
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockReturnValueOnce(
      new Promise<Response>((res) => { resolveReq = res; }),
    );

    const { result } = renderHook(() => useWikiMaintain("ch-1"));

    act(() => {
      void result.current.maintain();
    });

    expect(result.current.loading).toBe(true);

    await act(async () => {
      resolveReq(makeFetchResponse({ rewritten: 3, errors: 0 }));
      await Promise.resolve();
    });

    expect(result.current.loading).toBe(false);
  });

  it("populates result on success", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockResolvedValueOnce(
      makeFetchResponse({ rewritten: 5, errors: 1 }),
    );

    const { result } = renderHook(() => useWikiMaintain("ch-1"));

    await act(async () => {
      await result.current.maintain();
    });

    expect(result.current.result).toEqual({ rewritten: 5, errors: 1 });
    expect(result.current.error).toBeNull();
  });

  it("sets error on non-ok response", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockResolvedValueOnce(
      makeFetchResponse({ error: { code: "INTERNAL", message: "server error" } }, false, 500),
    );

    const { result } = renderHook(() => useWikiMaintain("ch-1"));

    await act(async () => {
      await result.current.maintain();
    });

    expect(result.current.error).toBeTruthy();
    expect(result.current.result).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it("sets error on network failure", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));

    const { result } = renderHook(() => useWikiMaintain("ch-1"));

    await act(async () => {
      await result.current.maintain();
    });

    expect(result.current.error).toBeTruthy();
    expect(result.current.loading).toBe(false);
  });

  it("does nothing when channelId is null", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);

    const { result } = renderHook(() => useWikiMaintain(null));

    await act(async () => {
      await result.current.maintain();
    });

    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.current.result).toBeNull();
  });
});
