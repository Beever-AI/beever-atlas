import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { EmbeddingTab } from "../EmbeddingTab";

function makeResponse(body: unknown, ok = true, status = 200): Response {
  return {
    ok,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

const EMBEDDING_ENDPOINT = {
  id: "ep-jina",
  name: "Jina prod",
  preset: "jina_ai",
  base_url: "https://api.jina.ai/v1",
  auth_type: "api_key",
  has_credential: true,
  credential_masked: "ji-...abcd",
  models: ["jina-embeddings-v4", "jina-embeddings-v3"],
  rpm: 60,
  headers: {},
  tags: [],
  last_test_at: null,
  last_test_ok: null,
  last_test_error: null,
  created_at: "2026-05-12T00:00:00Z",
  updated_at: "2026-05-12T00:00:00Z",
};

const OPENAI_EMB_ENDPOINT = {
  ...EMBEDDING_ENDPOINT,
  id: "ep-openai",
  name: "OpenAI emb",
  preset: "openai",
  base_url: "https://api.openai.com/v1",
  models: ["text-embedding-3-large", "text-embedding-3-small"],
};

function mkAssignment(consumer: string, endpoint_id: string, model: string, dimensions: number | null) {
  return {
    consumer,
    endpoint_id,
    model,
    temperature: null,
    max_tokens: null,
    response_format: null,
    extra_headers: {},
    fallback_endpoint_id: null,
    dimensions,
    task: null,
    updated_at: "2026-05-12T00:00:00Z",
  };
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

const STATE_OK = {
  migration_required: false,
  desired_provider: "jina_ai",
  desired_model: "jina-embeddings-v4",
  desired_dimensions: 2048,
  persisted_provider: "jina_ai",
  persisted_model: "jina-embeddings-v4",
  persisted_dimensions: 2048,
  fact_count: 0,
  reembed_supported: true,
  reason: null,
};

function renderTab() {
  return render(
    <MemoryRouter>
      <EmbeddingTab />
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

describe("EmbeddingTab", () => {
  it("renders the endpoint picker + model select + dimensions", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockImplementation(async (input: any) => {
      const url = String(input);
      if (url.includes("/api/settings/embedding-migration/status")) return makeResponse(IDLE_STATUS);
      if (url.includes("/api/settings/embedding-migration/state")) return makeResponse(STATE_OK);
      if (url.includes("/api/settings/endpoints")) return makeResponse({ endpoints: [EMBEDDING_ENDPOINT, OPENAI_EMB_ENDPOINT] });
      if (url.includes("/api/settings/assignments"))
        return makeResponse({
          assignments: [mkAssignment("embedding", "ep-jina", "jina-embeddings-v4", 2048)],
          default_consumers: ["embedding"],
          capabilities: {},
        });
      return makeResponse({});
    });

    renderTab();

    const endpointSelect = await screen.findByLabelText("embedding endpoint");
    expect((endpointSelect as HTMLSelectElement).value).toBe("ep-jina");
    expect(screen.getByLabelText("embedding model")).toBeTruthy();
    const dims = screen.getByLabelText("embedding dimensions") as HTMLInputElement;
    expect(dims.value).toBe("2048");
  });

  it("changing the endpoint/model marks the form dirty (Save enabled)", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockImplementation(async (input: any) => {
      const url = String(input);
      if (url.includes("/api/settings/embedding-migration/status")) return makeResponse(IDLE_STATUS);
      if (url.includes("/api/settings/embedding-migration/state")) return makeResponse(STATE_OK);
      if (url.includes("/api/settings/endpoints")) return makeResponse({ endpoints: [EMBEDDING_ENDPOINT, OPENAI_EMB_ENDPOINT] });
      if (url.includes("/api/settings/assignments"))
        return makeResponse({
          assignments: [mkAssignment("embedding", "ep-jina", "jina-embeddings-v4", 2048)],
          default_consumers: ["embedding"],
          capabilities: {},
        });
      return makeResponse({});
    });

    renderTab();
    await screen.findByLabelText("embedding endpoint");

    // Save is disabled until something changes.
    const saveBtn = screen.getByRole("button", { name: /Save Changes/i });
    expect((saveBtn as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(screen.getByLabelText("embedding model"), { target: { value: "jina-embeddings-v3" } });

    await waitFor(() => expect((screen.getByRole("button", { name: /Save Changes/i }) as HTMLButtonElement).disabled).toBe(false));
  });

  it("Save calls asn.upsert('embedding', …) without a legacy embedding PUT", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    let upsertBody: any = null;
    let legacyEmbeddingPut = false;
    fetchMock.mockImplementation(async (input: any, init?: any) => {
      const url = String(input);
      if (url.includes("/api/settings/assignments/embedding") && init?.method === "PUT") {
        upsertBody = JSON.parse(String(init.body));
        return makeResponse(mkAssignment("embedding", "ep-openai", "text-embedding-3-large", 3072));
      }
      // Guard: the tab must NOT PUT the legacy embedding-config endpoint.
      if (/\/api\/settings\/embedding($|\?)/.test(url) && init?.method === "PUT") legacyEmbeddingPut = true;
      if (url.includes("/api/settings/embedding-migration/status")) return makeResponse(IDLE_STATUS);
      if (url.includes("/api/settings/embedding-migration/state")) return makeResponse(STATE_OK);
      if (url.includes("/api/settings/endpoints")) return makeResponse({ endpoints: [EMBEDDING_ENDPOINT, OPENAI_EMB_ENDPOINT] });
      if (url.includes("/api/settings/assignments"))
        return makeResponse({
          assignments: [mkAssignment("embedding", "ep-jina", "jina-embeddings-v4", 2048)],
          default_consumers: ["embedding"],
          capabilities: {},
        });
      return makeResponse({});
    });

    renderTab();
    await screen.findByLabelText("embedding endpoint");

    fireEvent.change(screen.getByLabelText("embedding endpoint"), { target: { value: "ep-openai" } });
    await waitFor(() => expect((screen.getByLabelText("embedding endpoint") as HTMLSelectElement).value).toBe("ep-openai"));

    fireEvent.click(screen.getByRole("button", { name: /Save Changes/i }));

    await waitFor(() => expect(upsertBody).not.toBeNull());
    expect(upsertBody.endpoint_id).toBe("ep-openai");
    expect(upsertBody.model).toBe("text-embedding-3-large");
    expect(legacyEmbeddingPut).toBe(false);
  });

  it("a running migration shows the amber progress bar with % / ETA", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockImplementation(async (input: any) => {
      const url = String(input);
      if (url.includes("/api/settings/embedding-migration/status"))
        return makeResponse({ ...IDLE_STATUS, running: true, processed: 50, total: 200, stage: "embedding", started_at: new Date(Date.now() - 60_000).toISOString() });
      if (url.includes("/api/settings/embedding-migration/state")) return makeResponse(STATE_OK);
      if (url.includes("/api/settings/endpoints")) return makeResponse({ endpoints: [EMBEDDING_ENDPOINT] });
      if (url.includes("/api/settings/assignments"))
        return makeResponse({
          assignments: [mkAssignment("embedding", "ep-jina", "jina-embeddings-v4", 2048)],
          default_consumers: ["embedding"],
          capabilities: {},
        });
      return makeResponse({});
    });

    renderTab();
    await waitFor(() => expect(screen.getByText(/Re-embedding in progress/i)).toBeTruthy());
    // 50 / 200 = 25%.
    expect(screen.getByText(/25%/)).toBeTruthy();
    expect(screen.getByText(/50 \/ 200 rows/)).toBeTruthy();
  });

  it("migration_required shows the 'Re-embed required' banner with a Start button", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockImplementation(async (input: any) => {
      const url = String(input);
      if (url.includes("/api/settings/embedding-migration/status")) return makeResponse(IDLE_STATUS);
      if (url.includes("/api/settings/embedding-migration/state"))
        return makeResponse({
          ...STATE_OK,
          migration_required: true,
          persisted_provider: "openai",
          persisted_model: "text-embedding-3-large",
          persisted_dimensions: 3072,
          fact_count: 999,
        });
      if (url.includes("/api/settings/endpoints")) return makeResponse({ endpoints: [EMBEDDING_ENDPOINT] });
      if (url.includes("/api/settings/assignments"))
        return makeResponse({
          assignments: [mkAssignment("embedding", "ep-jina", "jina-embeddings-v4", 2048)],
          default_consumers: ["embedding"],
          capabilities: {},
        });
      return makeResponse({});
    });

    renderTab();
    // Wait for the form to hydrate (assignment + endpoints loaded → draft set)
    // *and* the migration state to resolve before reading the Start button —
    // the button is disabled while ``draft`` is null too, not only when
    // ``reembed_supported`` is false.
    await screen.findByLabelText("embedding endpoint");
    await waitFor(() => expect(screen.getByText("Re-embed required")).toBeTruthy());
    await waitFor(() => {
      const startBtn = screen.getByRole("button", { name: /Start re-embed/i });
      expect((startBtn as HTMLButtonElement).disabled).toBe(false);
    });
  });

  it("disables 'Start re-embed' + shows the reason when reembed_supported is false", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockImplementation(async (input: any) => {
      const url = String(input);
      if (url.includes("/api/settings/embedding-migration/status")) return makeResponse(IDLE_STATUS);
      if (url.includes("/api/settings/embedding-migration/state"))
        return makeResponse({
          ...STATE_OK,
          migration_required: true,
          persisted_provider: "openai",
          persisted_model: "text-embedding-3-large",
          persisted_dimensions: 3072,
          fact_count: 999,
          reembed_supported: false,
          reason: "endpoint preset 'anthropic' isn't a direct embedding provider — re-embed not yet supported via proxy endpoints",
        });
      if (url.includes("/api/settings/endpoints")) return makeResponse({ endpoints: [EMBEDDING_ENDPOINT] });
      if (url.includes("/api/settings/assignments"))
        return makeResponse({
          assignments: [mkAssignment("embedding", "ep-jina", "jina-embeddings-v4", 2048)],
          default_consumers: ["embedding"],
          capabilities: {},
        });
      return makeResponse({});
    });

    renderTab();
    await waitFor(() => expect(screen.getByText("Re-embed required")).toBeTruthy());
    const startBtn = screen.getByRole("button", { name: /Start re-embed/i });
    expect((startBtn as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/isn't a direct embedding provider/i)).toBeTruthy();
  });

  it("Test Connection shows an inline pass/fail banner", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockImplementation(async (input: any, init?: any) => {
      const url = String(input);
      if (url.includes("/api/settings/endpoints/ep-jina/test") && init?.method === "POST") {
        return makeResponse({ ok: true, latency_ms: 123, error: null });
      }
      if (url.includes("/api/settings/embedding-migration/status")) return makeResponse(IDLE_STATUS);
      if (url.includes("/api/settings/embedding-migration/state")) return makeResponse(STATE_OK);
      if (url.includes("/api/settings/endpoints")) return makeResponse({ endpoints: [EMBEDDING_ENDPOINT] });
      if (url.includes("/api/settings/assignments"))
        return makeResponse({
          assignments: [mkAssignment("embedding", "ep-jina", "jina-embeddings-v4", 2048)],
          default_consumers: ["embedding"],
          capabilities: {},
        });
      return makeResponse({});
    });

    renderTab();
    await screen.findByLabelText("embedding endpoint");

    fireEvent.click(screen.getByRole("button", { name: /Test Connection/i }));
    await waitFor(() => expect(screen.getByText(/Test passed/i)).toBeTruthy());
    expect(screen.getByText(/123 ms/)).toBeTruthy();
  });
});
