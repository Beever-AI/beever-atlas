import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { AgentModelsTab } from "../AgentModelsTab";

function makeResponse(body: unknown, ok = true, status = 200): Response {
  return {
    ok,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

const ENDPOINT = {
  id: "ep-1",
  name: "OpenAI prod",
  preset: "openai",
  base_url: "https://api.openai.com/v1",
  auth_type: "api_key",
  has_credential: true,
  credential_masked: "sk-p...1234",
  models: ["gpt-4o-mini", "gpt-4o", "o4-mini"],
  rpm: 500,
  headers: {},
  tags: [],
  last_test_at: null,
  last_test_ok: null,
  last_test_error: null,
  created_at: "2026-05-12T00:00:00Z",
  updated_at: "2026-05-12T00:00:00Z",
};

const DEFAULT_CONSUMERS = [
  "embedding",
  "fact_extractor",
  "entity_extractor",
  "cross_batch_validator",
  "coreference_resolver",
  "contradiction_detector",
  "image_describer",
  "video_analyzer",
  "audio_transcriber",
  "summarizer",
  "document_digester",
  "echo",
  "wiki_compiler",
  "wiki_maintainer",
  "qa_agent",
  "qa_router",
  "csv_mapper",
];

function mkAssignment(consumer: string, endpoint_id: string, model: string) {
  return {
    consumer,
    endpoint_id,
    model,
    temperature: null,
    max_tokens: null,
    response_format: null,
    extra_headers: {},
    fallback_endpoint_id: null,
    dimensions: null,
    task: null,
    updated_at: "2026-05-12T00:00:00Z",
  };
}

const ASSIGNMENTS_PAYLOAD = {
  assignments: [
    mkAssignment("qa_agent", "ep-1", "gpt-4o"),
    // image_describer needs vision; o4-mini has supports_vision:false → incompatible.
    mkAssignment("image_describer", "ep-1", "o4-mini"),
  ],
  default_consumers: DEFAULT_CONSUMERS,
  capabilities: { qa_agent: ["tools"], image_describer: ["vision"] },
};

function renderTab() {
  return render(
    <MemoryRouter>
      <AgentModelsTab />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("AgentModelsTab", () => {
  it("renders preset cards + agent groups + agent rows (grouped, 16 agents)", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockImplementation(async (input: any) => {
      const url = String(input);
      if (url.includes("/api/settings/endpoints")) return makeResponse({ endpoints: [ENDPOINT] });
      if (url.includes("/api/settings/assignments")) return makeResponse(ASSIGNMENTS_PAYLOAD);
      return makeResponse({});
    });

    renderTab();

    await waitFor(() => expect(screen.getByText("Gemini balanced")).toBeTruthy());
    // Preset cards (custom is skipped).
    expect(screen.getByText("OpenAI quality")).toBeTruthy();
    expect(screen.getByText("Fully local (Ollama)")).toBeTruthy();
    // No card labelled exactly "Custom".
    expect(screen.queryByText("Custom")).toBeNull();

    // Group headers — all six groups present (ingestion, media, post, wiki, qa, other).
    expect(screen.getByText("Ingestion Pipeline")).toBeTruthy();
    expect(screen.getByText("Media Processing")).toBeTruthy();
    expect(screen.getByText("Post-Processing")).toBeTruthy();
    expect(screen.getByText("Wiki Generation")).toBeTruthy();
    expect(screen.getByText("QA / Ask")).toBeTruthy();
    expect(screen.getByText("Other")).toBeTruthy();

    // Ingestion group is open by default → its rows are mounted.
    expect(await screen.findByLabelText("fact_extractor endpoint")).toBeTruthy();
    expect(screen.getByLabelText("entity_extractor endpoint")).toBeTruthy();

    // Each group toggles independently — expand the QA group and see its rows.
    fireEvent.click(screen.getByText("QA / Ask").closest("button")!);
    expect(await screen.findByLabelText("qa_agent endpoint")).toBeTruthy();
    expect(screen.getByLabelText("qa_router endpoint")).toBeTruthy();

    // wiki_maintainer (added in PR3) lives under Wiki Generation.
    fireEvent.click(screen.getByText("Wiki Generation").closest("button")!);
    expect(await screen.findByLabelText("wiki_maintainer endpoint")).toBeTruthy();
    expect(screen.getByLabelText("wiki_compiler endpoint")).toBeTruthy();

    // csv_mapper + echo grouped under "Other".
    fireEvent.click(screen.getByText("Other").closest("button")!);
    expect(await screen.findByLabelText("csv_mapper endpoint")).toBeTruthy();
    expect(screen.getByLabelText("echo endpoint")).toBeTruthy();
  });

  it("changing a model <select> calls asn.upsert with {endpoint_id, model}", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    let putBody: any = null;
    fetchMock.mockImplementation(async (input: any, init?: any) => {
      const url = String(input);
      if (url.includes("/api/settings/assignments/") && init?.method === "PUT") {
        putBody = JSON.parse(String(init.body));
        return makeResponse(mkAssignment("qa_agent", "ep-1", "gpt-4o-mini"));
      }
      if (url.includes("/api/settings/endpoints")) return makeResponse({ endpoints: [ENDPOINT] });
      if (url.includes("/api/settings/assignments")) return makeResponse(ASSIGNMENTS_PAYLOAD);
      return makeResponse({});
    });

    renderTab();
    await waitFor(() => expect(screen.getByText("QA / Ask")).toBeTruthy());
    // QA group is collapsed by default — expand it.
    fireEvent.click(screen.getByText("QA / Ask").closest("button")!);

    const modelSelect = await screen.findByLabelText("qa_agent model");
    fireEvent.change(modelSelect, { target: { value: "gpt-4o-mini" } });

    await waitFor(() => expect(putBody).not.toBeNull());
    expect(putBody.endpoint_id).toBe("ep-1");
    expect(putBody.model).toBe("gpt-4o-mini");
  });

  it("clicking a preset card calls applyPreset and shows the diff toast", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockImplementation(async (input: any, init?: any) => {
      const url = String(input);
      if (url.includes("/api/settings/assignments/preset") && init?.method === "POST") {
        return makeResponse({
          action: "applied",
          diff: [{ consumer: "qa_agent", before: null, after: mkAssignment("qa_agent", "ep-1", "gemini-2.5-flash") }],
          preserved: [],
        });
      }
      if (url.includes("/api/settings/endpoints")) return makeResponse({ endpoints: [ENDPOINT] });
      if (url.includes("/api/settings/assignments")) return makeResponse(ASSIGNMENTS_PAYLOAD);
      return makeResponse({});
    });

    renderTab();
    await waitFor(() => expect(screen.getByText("Gemini balanced")).toBeTruthy());
    fireEvent.click(screen.getByText("Gemini balanced"));

    // CI runners are slower than local — the toast lands after the POST
    // returns + a setState flush. 5000ms was not enough headroom (failed at
    // 5082ms on a GitHub runner on 2026-06-01); give the inner waitFor 10s
    // and keep the outer test budget above it.
    await waitFor(
      () => expect(screen.getByText(/Applied 'Gemini balanced' — 1 updated/)).toBeTruthy(),
      { timeout: 10000 },
    );
  }, 20000);

  it("a vision-required consumer with a no-vision model shows the red capability badge", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockImplementation(async (input: any) => {
      const url = String(input);
      if (url.includes("/api/settings/endpoints")) return makeResponse({ endpoints: [ENDPOINT] });
      if (url.includes("/api/settings/assignments")) return makeResponse(ASSIGNMENTS_PAYLOAD);
      return makeResponse({});
    });

    const { container } = renderTab();
    await waitFor(() => expect(screen.getByText("Media Processing")).toBeTruthy());
    fireEvent.click(screen.getByText("Media Processing").closest("button")!);

    await screen.findByLabelText("image_describer model");
    // The vision badge for image_describer should be flagged incompatible (o4-mini → no vision).
    await waitFor(() => {
      const badge = container.querySelector('[data-capability="vision"][data-incompatible="true"]');
      expect(badge).not.toBeNull();
    });
  });

  it("zero endpoints renders the empty-state CTA and the preset chips", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockImplementation(async (input: any) => {
      const url = String(input);
      if (url.includes("/api/settings/endpoints")) return makeResponse({ endpoints: [] });
      if (url.includes("/api/settings/assignments"))
        return makeResponse({ assignments: [], default_consumers: DEFAULT_CONSUMERS, capabilities: {} });
      return makeResponse({});
    });

    renderTab();
    await waitFor(() => expect(screen.getByText(/Add your first endpoint to get started/i)).toBeTruthy());
    expect(screen.getByText(/…or apply a preset:/)).toBeTruthy();
    // Preset chips inside the empty state.
    expect(screen.getAllByText("Gemini balanced").length).toBeGreaterThan(0);
    // No agent group sections rendered when there are no endpoints.
    expect(screen.queryByText("Ingestion Pipeline")).toBeNull();
  });
});
