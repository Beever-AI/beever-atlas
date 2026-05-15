import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useWikiLint } from "../useWikiLint";
import type { LintReport } from "../useWikiLint";

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

const MOCK_REPORT: LintReport = {
  channel_id: "ch-1",
  target_lang: "en",
  pages_scanned: 5,
  generated_at: new Date().toISOString(),
  findings: [
    {
      severity: "warning",
      category: "orphan",
      page_id: "topic-foo",
      message: "Page has no inbound links",
    },
    {
      severity: "error",
      category: "stale",
      page_id: "topic-bar",
      section_id: "summary",
      message: "Content is older than 30 days",
      suggested_action: "Regenerate this page",
    },
  ],
};

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

describe("useWikiLint", () => {
  it("starts with report=null, loading=false, error=null", () => {
    const { result } = renderHook(() => useWikiLint("ch-1"));
    expect(result.current.report).toBeNull();
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it("transitions loading=true while runLint is in flight", async () => {
    let resolveReq!: (v: Response) => void;
    vi.mocked(globalThis.fetch).mockReturnValueOnce(
      new Promise<Response>((res) => { resolveReq = res; }),
    );

    const { result } = renderHook(() => useWikiLint("ch-1"));

    act(() => {
      void result.current.runLint();
    });

    expect(result.current.loading).toBe(true);

    await act(async () => {
      resolveReq(makeFetchResponse(MOCK_REPORT));
      await Promise.resolve();
    });

    expect(result.current.loading).toBe(false);
  });

  it("populates report on success", async () => {
    vi.mocked(globalThis.fetch).mockResolvedValueOnce(
      makeFetchResponse(MOCK_REPORT),
    );

    const { result } = renderHook(() => useWikiLint("ch-1"));

    await act(async () => {
      await result.current.runLint();
    });

    expect(result.current.report).toEqual(MOCK_REPORT);
    expect(result.current.error).toBeNull();
  });

  it("populates error on server error response", async () => {
    vi.mocked(globalThis.fetch).mockResolvedValueOnce(
      makeFetchResponse(
        { error: { code: "INTERNAL", message: "server error" } },
        false,
        500,
      ),
    );

    const { result } = renderHook(() => useWikiLint("ch-1"));

    await act(async () => {
      await result.current.runLint();
    });

    expect(result.current.error).toBeTruthy();
    expect(result.current.report).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it("populates error on network failure", async () => {
    vi.mocked(globalThis.fetch).mockRejectedValueOnce(new TypeError("Failed to fetch"));

    const { result } = renderHook(() => useWikiLint("ch-1"));

    await act(async () => {
      await result.current.runLint();
    });

    expect(result.current.error).toBeTruthy();
    expect(result.current.loading).toBe(false);
  });

  it("clear() resets report and error", async () => {
    vi.mocked(globalThis.fetch).mockResolvedValueOnce(
      makeFetchResponse(MOCK_REPORT),
    );

    const { result } = renderHook(() => useWikiLint("ch-1"));

    await act(async () => {
      await result.current.runLint();
    });

    expect(result.current.report).not.toBeNull();

    act(() => {
      result.current.clear();
    });

    expect(result.current.report).toBeNull();
    expect(result.current.error).toBeNull();
  });

  it("does nothing when channelId is null", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);

    const { result } = renderHook(() => useWikiLint(null));

    await act(async () => {
      await result.current.runLint();
    });

    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.current.report).toBeNull();
  });
});
