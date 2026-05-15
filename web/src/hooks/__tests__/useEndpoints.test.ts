import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useEndpoints } from "../useEndpoints";
import type { Endpoint } from "@/lib/aiSetup";

function makeResponse(body: unknown, ok = true, status = 200): Response {
  return {
    ok,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

const MOCK_ENDPOINT: Endpoint = {
  id: "ep-abc",
  name: "OpenAI prod",
  preset: "openai",
  base_url: "https://api.openai.com/v1",
  auth_type: "api_key",
  has_credential: true,
  credential_masked: "sk-p...AbCd",
  models: ["gpt-4o-mini"],
  rpm: 500,
  headers: {},
  tags: [],
  last_test_at: null,
  last_test_ok: null,
  last_test_error: null,
  created_at: "2026-05-12T00:00:00Z",
  updated_at: "2026-05-12T00:00:00Z",
};

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("useEndpoints", () => {
  it("fetches endpoints on mount", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockResolvedValue(
      makeResponse({ endpoints: [MOCK_ENDPOINT] })
    );

    const { result } = renderHook(() => useEndpoints());

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });
    expect(result.current.endpoints).toHaveLength(1);
    expect(result.current.endpoints[0].id).toBe("ep-abc");
  });

  it("create posts to /api/settings/endpoints + refetches", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    // Initial GET, POST create, refetch GET.
    fetchMock
      .mockResolvedValueOnce(makeResponse({ endpoints: [] }))
      .mockResolvedValueOnce(makeResponse(MOCK_ENDPOINT))
      .mockResolvedValueOnce(makeResponse({ endpoints: [MOCK_ENDPOINT] }));

    const { result } = renderHook(() => useEndpoints());
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    await act(async () => {
      await result.current.create({
        name: "OpenAI prod",
        preset: "openai",
        api_key: "sk-test",
      });
    });

    expect(fetchMock).toHaveBeenCalledTimes(3);
    const postCall = fetchMock.mock.calls[1];
    expect(String(postCall[0])).toContain("/api/settings/endpoints");
    expect(postCall[1]?.method).toBe("POST");
    expect(result.current.endpoints).toHaveLength(1);
  });

  it("test endpoint hits /test sub-route", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock
      .mockResolvedValueOnce(makeResponse({ endpoints: [MOCK_ENDPOINT] }))
      .mockResolvedValueOnce(
        makeResponse({ ok: true, latency_ms: 312, error: null })
      );

    const { result } = renderHook(() => useEndpoints());
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    let testResult: any;
    await act(async () => {
      testResult = await result.current.test("ep-abc");
    });

    expect(testResult.ok).toBe(true);
    expect(testResult.latency_ms).toBe(312);
    const testCall = fetchMock.mock.calls[1];
    expect(String(testCall[0])).toContain("/api/settings/endpoints/ep-abc/test");
  });

  it("discover hits /discover sub-route", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock
      .mockResolvedValueOnce(makeResponse({ endpoints: [MOCK_ENDPOINT] }))
      .mockResolvedValueOnce(
        makeResponse({ ok: true, models: ["gpt-4o-mini", "gpt-4o"], error: null })
      );

    const { result } = renderHook(() => useEndpoints());
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    let discovered: any;
    await act(async () => {
      discovered = await result.current.discover("ep-abc");
    });

    expect(discovered.ok).toBe(true);
    expect(discovered.models).toContain("gpt-4o");
  });

  it("delete removes the endpoint + refetches", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock
      .mockResolvedValueOnce(makeResponse({ endpoints: [MOCK_ENDPOINT] }))
      .mockResolvedValueOnce(makeResponse({}, true, 204))
      .mockResolvedValueOnce(makeResponse({ endpoints: [] }));

    const { result } = renderHook(() => useEndpoints());
    await waitFor(() => expect(result.current.endpoints).toHaveLength(1));

    await act(async () => {
      await result.current.remove("ep-abc");
    });

    expect(result.current.endpoints).toHaveLength(0);
    const deleteCall = fetchMock.mock.calls[1];
    expect(deleteCall[1]?.method).toBe("DELETE");
  });

  it("surfaces error on fetch failure", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockResolvedValue(
      makeResponse({ detail: "boom" }, false, 500)
    );

    const { result } = renderHook(() => useEndpoints());
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    expect(result.current.error).not.toBeNull();
  });
});
