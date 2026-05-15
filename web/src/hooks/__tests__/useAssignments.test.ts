import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useAssignments } from "../useAssignments";
import type { Assignment } from "@/lib/aiSetup";

function makeResponse(body: unknown, ok = true, status = 200): Response {
  return {
    ok,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

const MOCK_ASSIGNMENT: Assignment = {
  consumer: "qa_agent",
  endpoint_id: "ep-1",
  model: "claude-sonnet-4-6",
  temperature: 0.2,
  max_tokens: null,
  response_format: null,
  extra_headers: {},
  fallback_endpoint_id: null,
  dimensions: null,
  task: null,
  updated_at: "2026-05-12T00:00:00Z",
};

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("useAssignments", () => {
  it("fetches assignments + capabilities on mount", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockResolvedValue(
      makeResponse({
        assignments: [MOCK_ASSIGNMENT],
        default_consumers: ["qa_agent", "fact_extractor"],
        capabilities: { qa_agent: ["tools"], image_describer: ["vision"] },
      })
    );

    const { result } = renderHook(() => useAssignments());
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    expect(result.current.assignments).toHaveLength(1);
    expect(result.current.defaultConsumers).toContain("fact_extractor");
    expect(result.current.capabilities["qa_agent"]).toEqual(["tools"]);
  });

  it("upsert puts to /api/settings/assignments/{consumer} + refetches", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock
      .mockResolvedValueOnce(
        makeResponse({
          assignments: [],
          default_consumers: [],
          capabilities: {},
        })
      )
      .mockResolvedValueOnce(makeResponse(MOCK_ASSIGNMENT))
      .mockResolvedValueOnce(
        makeResponse({
          assignments: [MOCK_ASSIGNMENT],
          default_consumers: [],
          capabilities: {},
        })
      );

    const { result } = renderHook(() => useAssignments());
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    await act(async () => {
      await result.current.upsert("qa_agent", {
        endpoint_id: "ep-1",
        model: "claude-sonnet-4-6",
        temperature: 0.2,
      });
    });

    const putCall = fetchMock.mock.calls[1];
    expect(String(putCall[0])).toContain("/api/settings/assignments/qa_agent");
    expect(putCall[1]?.method).toBe("PUT");
    expect(result.current.assignments).toHaveLength(1);
  });

  it("previewPreset posts confirm=false", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock
      .mockResolvedValueOnce(
        makeResponse({ assignments: [], default_consumers: [], capabilities: {} })
      )
      .mockResolvedValueOnce(
        makeResponse({ action: "preview", diff: [], preserved: [] })
      );

    const { result } = renderHook(() => useAssignments());
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    let preview: any;
    await act(async () => {
      preview = await result.current.previewPreset("gemini-balanced");
    });

    expect(preview.action).toBe("preview");
    const postCall = fetchMock.mock.calls[1];
    const body = JSON.parse(String(postCall[1]?.body));
    expect(body.confirm).toBe(false);
  });

  it("applyPreset posts confirm=true + refetches", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock
      .mockResolvedValueOnce(
        makeResponse({ assignments: [], default_consumers: [], capabilities: {} })
      )
      .mockResolvedValueOnce(
        makeResponse({ action: "applied", diff: [], preserved: [] })
      )
      .mockResolvedValueOnce(
        makeResponse({
          assignments: [MOCK_ASSIGNMENT],
          default_consumers: [],
          capabilities: {},
        })
      );

    const { result } = renderHook(() => useAssignments());
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    let applied: any;
    await act(async () => {
      applied = await result.current.applyPreset("gemini-balanced");
    });

    expect(applied.action).toBe("applied");
    const postBody = JSON.parse(String(fetchMock.mock.calls[1][1]?.body));
    expect(postBody.confirm).toBe(true);
    expect(result.current.assignments).toHaveLength(1);
  });

  it("applyPreset propagates force_overwrite_custom", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock
      .mockResolvedValueOnce(
        makeResponse({ assignments: [], default_consumers: [], capabilities: {} })
      )
      .mockResolvedValueOnce(
        makeResponse({ action: "applied", diff: [], preserved: [] })
      )
      .mockResolvedValueOnce(
        makeResponse({ assignments: [], default_consumers: [], capabilities: {} })
      );

    const { result } = renderHook(() => useAssignments());
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    await act(async () => {
      await result.current.applyPreset("openai-quality", true);
    });

    const body = JSON.parse(String(fetchMock.mock.calls[1][1]?.body));
    expect(body.force_overwrite_custom).toBe(true);
  });
});
