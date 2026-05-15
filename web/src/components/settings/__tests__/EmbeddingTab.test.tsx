import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { EmbeddingTab } from "../EmbeddingTab";
import type { Assignment, Endpoint } from "@/lib/aiSetup";
import type { EmbeddingMigrationStatus, EmbeddingReembedState } from "@/lib/types";

function makeResponse(body: unknown, ok = true, status = 200): Response {
  return {
    ok,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

const EMBEDDING_ENDPOINT: Endpoint = {
  id: "ep-jina",
  name: "Jina prod",
  preset: "jina_ai",
  base_url: "https://api.jina.ai/v1",
  auth_type: "api_key",
  has_credential: true,
  credential_masked: "ji-...abcd",
  // The endpoint advertises *chat-ish* model names too — the tab must NOT
  // source the Model picker from this list.
  models: ["jina-embeddings-v4", "jina-embeddings-v3", "jina-reranker-v2"],
  rpm: 60,
  headers: {},
  tags: [],
  last_test_at: null,
  last_test_ok: null,
  last_test_error: null,
  created_at: "2026-05-12T00:00:00Z",
  updated_at: "2026-05-12T00:00:00Z",
};

const GOOGLE_AI_ENDPOINT = {
  ...EMBEDDING_ENDPOINT,
  id: "ep-google",
  // An env-hydrated endpoint: noisy auto-generated name + migrated-from-env tag.
  name: "google_ai (from GOOGLE_API_KEY)",
  preset: "google_ai",
  base_url: "https://generativelanguage.googleapis.com/v1beta/openai/",
  // Chat models — the embedding picker must ignore these and offer
  // gemini-embedding-001 from KNOWN_EMBEDDING_MODELS instead.
  models: ["models/gemini-2.5-flash", "models/gemini-2.5-pro"],
  tags: ["migrated-from-env"],
};

const OPENAI_EMB_ENDPOINT = {
  ...EMBEDDING_ENDPOINT,
  id: "ep-openai",
  name: "OpenAI emb",
  preset: "openai",
  base_url: "https://api.openai.com/v1",
  models: ["gpt-4o-mini", "text-embedding-3-large"],
  tags: [],
};

function mkAssignment(consumer: string, endpoint_id: string, model: string, dimensions: number | null): Assignment {
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

const IDLE_STATUS: EmbeddingMigrationStatus = {
  running: false,
  job_id: null,
  stage: null,
  processed: null,
  total: null,
  started_at: null,
  finished_at: null,
  error: null,
};

const STATE_OK: EmbeddingReembedState = {
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

/** Build a fetch impl from a route → response map. Unmatched ⇒ {} . */
function mkFetch(routes: {
  endpoints?: Endpoint[];
  assignment?: Assignment;
  status?: EmbeddingMigrationStatus;
  state?: EmbeddingReembedState;
  onUpsert?: (body: { endpoint_id: string; model: string; dimensions?: number | null; task?: string | null }) => unknown;
  onTest?: (id: string) => unknown;
  onSpawn?: () => unknown;
}) {
  const endpoints = routes.endpoints ?? [EMBEDDING_ENDPOINT];
  const assignment = routes.assignment ?? mkAssignment("embedding", "ep-jina", "jina-embeddings-v4", 2048);
  const status = routes.status ?? IDLE_STATUS;
  const state = routes.state ?? STATE_OK;
  return async (input: any, init?: any): Promise<Response> => {
    const url = String(input);
    const method = init?.method ?? "GET";
    if (url.includes("/api/settings/assignments/embedding") && method === "PUT") {
      const body = JSON.parse(String(init.body));
      const r = routes.onUpsert?.(body) ?? mkAssignment("embedding", body.endpoint_id, body.model, body.dimensions ?? null);
      return makeResponse(r);
    }
    const testMatch = url.match(/\/api\/settings\/endpoints\/([^/]+)\/test/);
    if (testMatch && method === "POST") {
      return makeResponse(routes.onTest?.(testMatch[1]) ?? { ok: true, latency_ms: 99, error: null });
    }
    if (url.includes("/api/settings/embedding-migration/spawn") && method === "POST") {
      return makeResponse(routes.onSpawn?.() ?? { job_id: "job-1", status: "running" });
    }
    if (url.includes("/api/settings/embedding-migration/status")) return makeResponse(status);
    if (url.includes("/api/settings/embedding-migration/state")) return makeResponse(state);
    if (url.includes("/api/settings/endpoints")) return makeResponse({ endpoints });
    if (url.includes("/api/settings/assignments")) {
      return makeResponse({ assignments: [assignment], default_consumers: ["embedding"], capabilities: {} });
    }
    return makeResponse({});
  };
}

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
  it("renders the endpoint picker + a Model picker that lists embedding (not chat) models, and no Dimensions field", async () => {
    vi.mocked(globalThis.fetch).mockImplementation(
      mkFetch({ endpoints: [EMBEDDING_ENDPOINT, GOOGLE_AI_ENDPOINT, OPENAI_EMB_ENDPOINT] }),
    );

    renderTab();

    const endpointSelect = (await screen.findByLabelText("embedding endpoint")) as HTMLSelectElement;
    expect(endpointSelect.value).toBe("ep-jina");

    const modelSelect = screen.getByLabelText("embedding model") as HTMLSelectElement;
    const optionValues = Array.from(modelSelect.options).map((o) => o.value);
    // Embedding models for jina_ai are present…
    expect(optionValues).toContain("jina-embeddings-v4");
    expect(optionValues).toContain("jina-embeddings-v3");
    // …the endpoint's *non-embedding* model is NOT…
    expect(optionValues).not.toContain("jina-reranker-v2");
    // …and the custom escape hatch is present.
    expect(optionValues).toContain("__custom__");
    // The selected option carries a dim/cost label.
    const selectedLabel = modelSelect.options[modelSelect.selectedIndex].textContent ?? "";
    expect(selectedLabel).toMatch(/2048-dim/);
    expect(selectedLabel).toMatch(/multilingual/);

    // No Dimensions input anymore.
    expect(screen.queryByLabelText("embedding dimensions")).toBeNull();
    // The known dimension is surfaced read-only instead.
    expect(screen.getAllByText(/2048-dim/).length).toBeGreaterThan(0);
  });

  it("shows a clean endpoint label for env-hydrated endpoints (no raw '(google_ai)' suffix)", async () => {
    vi.mocked(globalThis.fetch).mockImplementation(
      mkFetch({ endpoints: [EMBEDDING_ENDPOINT, GOOGLE_AI_ENDPOINT] }),
    );
    renderTab();
    const endpointSelect = (await screen.findByLabelText("embedding endpoint")) as HTMLSelectElement;
    const labels = Array.from(endpointSelect.options).map((o) => o.textContent ?? "");
    expect(labels).toContain("Jina prod");
    expect(labels).toContain("Google AI (Gemini) (auto-detected)");
    expect(labels.some((l) => l.includes("(google_ai)"))).toBe(false);
    expect(labels.some((l) => l.includes("GOOGLE_API_KEY"))).toBe(false);
  });

  it("switching to the custom-model option reveals a free-text input", async () => {
    vi.mocked(globalThis.fetch).mockImplementation(mkFetch({}));
    renderTab();
    await screen.findByLabelText("embedding endpoint");
    expect(screen.queryByLabelText("custom embedding model")).toBeNull();
    fireEvent.change(screen.getByLabelText("embedding model"), { target: { value: "__custom__" } });
    await waitFor(() => expect(screen.getByLabelText("custom embedding model")).toBeTruthy());
  });

  it("changing the model marks the form dirty (Save enabled)", async () => {
    vi.mocked(globalThis.fetch).mockImplementation(mkFetch({}));
    renderTab();
    await screen.findByLabelText("embedding endpoint");

    const saveBtn = screen.getByRole("button", { name: /Save Changes/i }) as HTMLButtonElement;
    expect(saveBtn.disabled).toBe(true);

    fireEvent.change(screen.getByLabelText("embedding model"), { target: { value: "jina-embeddings-v3" } });
    await waitFor(() =>
      expect((screen.getByRole("button", { name: /Save Changes/i }) as HTMLButtonElement).disabled).toBe(false),
    );
  });

  it("Save calls asn.upsert('embedding', …) with the derived dimensions and no legacy embedding PUT", async () => {
    let upsertBody: any = null;
    let legacyEmbeddingPut = false;
    vi.mocked(globalThis.fetch).mockImplementation(async (input: any, init?: any) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url.includes("/api/settings/assignments/embedding") && method === "PUT") {
        upsertBody = JSON.parse(String(init.body));
        return makeResponse(mkAssignment("embedding", upsertBody.endpoint_id, upsertBody.model, upsertBody.dimensions ?? null));
      }
      if (/\/api\/settings\/embedding($|\?)/.test(url) && method === "PUT") legacyEmbeddingPut = true;
      return mkFetch({ endpoints: [EMBEDDING_ENDPOINT, OPENAI_EMB_ENDPOINT] })(input, init);
    });

    renderTab();
    await screen.findByLabelText("embedding endpoint");

    fireEvent.change(screen.getByLabelText("embedding endpoint"), { target: { value: "ep-openai" } });
    await waitFor(() =>
      expect((screen.getByLabelText("embedding endpoint") as HTMLSelectElement).value).toBe("ep-openai"),
    );
    // The model select should now offer OpenAI embedding models and default to one.
    await waitFor(() => {
      const ms = screen.getByLabelText("embedding model") as HTMLSelectElement;
      expect(Array.from(ms.options).map((o) => o.value)).toContain("text-embedding-3-large");
    });
    fireEvent.change(screen.getByLabelText("embedding model"), { target: { value: "text-embedding-3-large" } });

    fireEvent.click(screen.getByRole("button", { name: /Save Changes/i }));
    await waitFor(() => expect(upsertBody).not.toBeNull());
    expect(upsertBody.endpoint_id).toBe("ep-openai");
    expect(upsertBody.model).toBe("text-embedding-3-large");
    // dim of openai/text-embedding-3-large in KNOWN_EMBEDDING_MODELS.
    expect(upsertBody.dimensions).toBe(3072);
    expect(legacyEmbeddingPut).toBe(false);
  });

  it("Save with a custom model sends dimensions: null", async () => {
    let upsertBody: any = null;
    vi.mocked(globalThis.fetch).mockImplementation(async (input: any, init?: any) => {
      const url = String(input);
      if (url.includes("/api/settings/assignments/embedding") && (init?.method ?? "GET") === "PUT") {
        upsertBody = JSON.parse(String(init.body));
        return makeResponse(mkAssignment("embedding", upsertBody.endpoint_id, upsertBody.model, upsertBody.dimensions ?? null));
      }
      return mkFetch({})(input, init);
    });
    renderTab();
    await screen.findByLabelText("embedding endpoint");
    fireEvent.change(screen.getByLabelText("embedding model"), { target: { value: "__custom__" } });
    const customInput = await screen.findByLabelText("custom embedding model");
    fireEvent.change(customInput, { target: { value: "my-self-hosted-embed" } });
    fireEvent.click(screen.getByRole("button", { name: /Save Changes/i }));
    await waitFor(() => expect(upsertBody).not.toBeNull());
    expect(upsertBody.model).toBe("my-self-hosted-embed");
    expect(upsertBody.dimensions).toBeNull();
  });

  it("a running migration shows the amber progress bar with % and ETA, and locks the config form", async () => {
    vi.mocked(globalThis.fetch).mockImplementation(
      mkFetch({
        status: {
          ...IDLE_STATUS,
          running: true,
          processed: 50,
          total: 200,
          stage: "embedding",
          started_at: new Date(Date.now() - 60_000).toISOString(),
        },
      }),
    );
    renderTab();
    await waitFor(() => expect(screen.getByText(/Re-embedding in progress/i)).toBeTruthy());
    expect(screen.getByText(/25%/)).toBeTruthy();
    expect(screen.getByText(/50 \/ 200 facts/)).toBeTruthy();
    // The endpoint select is disabled while a job runs, and the lock note shows.
    const endpointSelect = await screen.findByLabelText("embedding endpoint");
    expect((endpointSelect as HTMLSelectElement).disabled).toBe(true);
    await waitFor(() => expect(screen.getByText(/locked while re-embedding/i)).toBeTruthy());
  });

  it("migration_required shows the 'Re-embed required' banner with a Start button", async () => {
    vi.mocked(globalThis.fetch).mockImplementation(
      mkFetch({
        state: {
          ...STATE_OK,
          migration_required: true,
          persisted_provider: "openai",
          persisted_model: "text-embedding-3-large",
          persisted_dimensions: 3072,
          fact_count: 999,
        },
      }),
    );
    renderTab();
    await screen.findByLabelText("embedding endpoint");
    await waitFor(() => expect(screen.getByText("Re-embed required")).toBeTruthy());
    await waitFor(() => {
      const startBtn = screen.getByRole("button", { name: /Start re-embed/i }) as HTMLButtonElement;
      expect(startBtn.disabled).toBe(false);
    });
    // Opening the modal previews the cost.
    fireEvent.click(screen.getByRole("button", { name: /Start re-embed/i }));
    await waitFor(() => expect(screen.getByRole("dialog", { name: /Confirm re-embed/i })).toBeTruthy());
  });

  it("a failed re-embed shows the red banner with a Retry button", async () => {
    vi.mocked(globalThis.fetch).mockImplementation(
      mkFetch({
        status: { ...IDLE_STATUS, error: "RateLimitError: 429 from jina" },
        state: { ...STATE_OK, fact_count: 100 },
      }),
    );
    renderTab();
    await screen.findByLabelText("embedding endpoint");
    await waitFor(() => expect(screen.getByText("Last re-embed failed")).toBeTruthy());
    expect(screen.getByText(/RateLimitError: 429 from jina/)).toBeTruthy();
    await waitFor(() => {
      const retryBtn = screen.getByRole("button", { name: /^Retry$/i }) as HTMLButtonElement;
      expect(retryBtn.disabled).toBe(false);
    });
  });

  it("disables 'Start re-embed' + shows the reason when reembed_supported is false", async () => {
    vi.mocked(globalThis.fetch).mockImplementation(
      mkFetch({
        state: {
          ...STATE_OK,
          migration_required: true,
          persisted_provider: "openai",
          persisted_model: "text-embedding-3-large",
          persisted_dimensions: 3072,
          fact_count: 999,
          reembed_supported: false,
          reason: "endpoint preset 'anthropic' isn't a direct embedding provider — re-embed not yet supported via proxy endpoints",
        },
      }),
    );
    renderTab();
    await waitFor(() => expect(screen.getByText("Re-embed required")).toBeTruthy());
    const startBtn = screen.getByRole("button", { name: /Start re-embed/i }) as HTMLButtonElement;
    expect(startBtn.disabled).toBe(true);
    expect(screen.getByText(/isn't a direct embedding provider/i)).toBeTruthy();
  });

  it("shows the quiet 'Embeddings up to date' pill when in sync", async () => {
    vi.mocked(globalThis.fetch).mockImplementation(
      mkFetch({ state: { ...STATE_OK, fact_count: 1234 } }),
    );
    renderTab();
    await waitFor(() => expect(screen.getByText(/Embeddings up to date/i)).toBeTruthy());
    expect(screen.getByText(/1,234 facts on/)).toBeTruthy();
  });

  it("shows the 'add an embedding provider' CTA when there are no embedding-capable endpoints", async () => {
    // anthropic isn't embedding-capable, so the list is empty.
    const anthropic = { ...EMBEDDING_ENDPOINT, id: "ep-claude", preset: "anthropic", name: "Claude prod" };
    vi.mocked(globalThis.fetch).mockImplementation(
      mkFetch({ endpoints: [anthropic], assignment: mkAssignment("embedding", "ep-claude", "x", null), state: { ...STATE_OK, persisted_provider: null, persisted_model: null } }),
    );
    renderTab();
    await waitFor(() => expect(screen.getByText(/Add an embedding provider to start/i)).toBeTruthy());
    expect(screen.getByRole("button", { name: /Add embedding endpoint/i })).toBeTruthy();
  });

  it("Test Connection shows an inline pass/fail banner", async () => {
    vi.mocked(globalThis.fetch).mockImplementation(
      mkFetch({ onTest: () => ({ ok: true, latency_ms: 123, error: null }) }),
    );
    renderTab();
    await screen.findByLabelText("embedding endpoint");
    fireEvent.click(screen.getByRole("button", { name: /Test Connection/i }));
    await waitFor(() => expect(screen.getByText(/Test passed/i)).toBeTruthy());
    expect(screen.getByText(/123 ms/)).toBeTruthy();
  });

  it("after Save, when the new dim differs from what's persisted, the re-embed confirm modal opens", async () => {
    let spawned = false;
    vi.mocked(globalThis.fetch).mockImplementation(async (input: any, init?: any) => {
      const url = String(input);
      if (url.includes("/api/settings/embedding-migration/spawn") && (init?.method ?? "GET") === "POST") {
        spawned = true;
        return makeResponse({ job_id: "j1", status: "running" });
      }
      return mkFetch({
        endpoints: [EMBEDDING_ENDPOINT, OPENAI_EMB_ENDPOINT],
        // persisted on openai/3072; switching to jina v4 (2048) differs → re-embed.
        state: {
          ...STATE_OK,
          persisted_provider: "openai",
          persisted_model: "text-embedding-3-large",
          persisted_dimensions: 3072,
          fact_count: 500,
        },
        assignment: mkAssignment("embedding", "ep-openai", "text-embedding-3-large", 3072),
      })(input, init);
    });
    renderTab();
    await screen.findByLabelText("embedding endpoint");
    // Switch endpoint to jina, model to v4.
    fireEvent.change(screen.getByLabelText("embedding endpoint"), { target: { value: "ep-jina" } });
    await waitFor(() =>
      expect((screen.getByLabelText("embedding endpoint") as HTMLSelectElement).value).toBe("ep-jina"),
    );
    fireEvent.click(screen.getByRole("button", { name: /Save Changes/i }));
    // The confirm modal pops with the cost preview.
    const dialog = await screen.findByRole("dialog", { name: /Confirm re-embed/i });
    expect(within(dialog).getByText(/500/)).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: /Start re-embed/i }));
    await waitFor(() => expect(spawned).toBe(true));
  });

  it("offers gemini-embedding-001 AND text-embedding-004 for a Google AI (Gemini) endpoint", async () => {
    vi.mocked(globalThis.fetch).mockImplementation(
      mkFetch({
        endpoints: [GOOGLE_AI_ENDPOINT, EMBEDDING_ENDPOINT],
        assignment: mkAssignment("embedding", "ep-google", "gemini-embedding-001", 3072),
        state: { ...STATE_OK, persisted_provider: null, persisted_model: null, fact_count: 0 },
      }),
    );
    renderTab();
    const modelSelect = (await screen.findByLabelText("embedding model")) as HTMLSelectElement;
    const optionValues = Array.from(modelSelect.options).map((o) => o.value);
    expect(optionValues).toContain("gemini-embedding-001");
    expect(optionValues).toContain("text-embedding-004");
    // The chat models from the endpoint's `models` list are NOT offered.
    expect(optionValues).not.toContain("models/gemini-2.5-flash");
    expect(optionValues).not.toContain("models/gemini-2.5-pro");
  });

  it("shows a 'Currently in use' line with the persisted provider/model", async () => {
    vi.mocked(globalThis.fetch).mockImplementation(
      mkFetch({
        state: {
          ...STATE_OK,
          persisted_provider: "jina_ai",
          persisted_model: "jina-embeddings-v4",
          persisted_dimensions: 2048,
          fact_count: 1494,
        },
      }),
    );
    renderTab();
    await screen.findByLabelText("embedding endpoint");
    const line = await waitFor(() => screen.getByText(/Currently in use:/i));
    // The reference line carries the persisted provider/model + fact count.
    expect(within(line).getByText("jina_ai/jina-embeddings-v4")).toBeTruthy();
    expect(line.textContent).toMatch(/1,494 facts/);
    expect(line.textContent).toMatch(/2048-dim/);
  });

  it("shows the amber 'Re-embed required' banner when the configured model differs from persisted, even though migration_required is false", async () => {
    // The trap from the bug report: the configured Assignment is a *chat* model
    // on google_ai, but Weaviate is running jina_ai/jina-embeddings-v4. The
    // backend's migration_required only compares dims (chat model dim unknown →
    // false), so the client-side name check is what surfaces the mismatch.
    vi.mocked(globalThis.fetch).mockImplementation(
      mkFetch({
        endpoints: [GOOGLE_AI_ENDPOINT, EMBEDDING_ENDPOINT],
        assignment: mkAssignment("embedding", "ep-google", "models/gemini-2.5-flash", null),
        state: {
          ...STATE_OK,
          migration_required: false,
          desired_provider: "gemini",
          desired_model: "models/gemini-2.5-flash",
          desired_dimensions: null,
          persisted_provider: "jina_ai",
          persisted_model: "jina-embeddings-v4",
          persisted_dimensions: 2048,
          fact_count: 1494,
        },
      }),
    );
    renderTab();
    await screen.findByLabelText("embedding endpoint");
    const heading = await waitFor(() => screen.getByText("Re-embed required"));
    // The banner names both sides of the mismatch.
    const banner = heading.closest("div.rounded-xl") as HTMLElement;
    expect(within(banner).getByText("gemini/models/gemini-2.5-flash")).toBeTruthy();
    expect(within(banner).getByText(/jina_ai\/jina-embeddings-v4/)).toBeTruthy();
    expect(banner.textContent).toMatch(/1,494 facts/);
    // …and the green "up to date" pill is NOT shown.
    expect(screen.queryByText(/Embeddings up to date/i)).toBeNull();
  });

  it("PR-γ: never offers chat models in the Model picker even when they're in endpoint.models[] (model_kinds-aware)", async () => {
    // Endpoint advertises a chat model + an embedding model and the
    // classifier has tagged them. The Model picker must not surface
    // gpt-4o-mini even though it's in endpoint.models[].
    const mixedOpenAI = {
      ...OPENAI_EMB_ENDPOINT,
      models: ["gpt-4o-mini", "gpt-4o", "text-embedding-3-large"],
      model_kinds: {
        "gpt-4o-mini": "chat",
        "gpt-4o": "chat",
        "text-embedding-3-large": "embedding",
      } as Record<string, "chat" | "embedding">,
    };
    vi.mocked(globalThis.fetch).mockImplementation(
      mkFetch({
        endpoints: [mixedOpenAI],
        assignment: mkAssignment("embedding", "ep-openai", "text-embedding-3-large", 3072),
        state: { ...STATE_OK, persisted_provider: "openai", persisted_model: "text-embedding-3-large", persisted_dimensions: 3072 },
      }),
    );
    renderTab();
    const modelSelect = (await screen.findByLabelText("embedding model")) as HTMLSelectElement;
    const optionValues = Array.from(modelSelect.options).map((o) => o.value);
    // Embedding model is offered.
    expect(optionValues).toContain("text-embedding-3-large");
    // Chat models are NOT offered.
    expect(optionValues).not.toContain("gpt-4o-mini");
    expect(optionValues).not.toContain("gpt-4o");
    // The custom escape hatch remains.
    expect(optionValues).toContain("__custom__");
  });

  it("PR-γ: shows '(no embedding models — run Discover)' on the endpoint option when classifier produced zero embedding kinds", async () => {
    // Jina endpoint, role=embedding, classifier ran but tagged zero models as
    // embedding (e.g. an upstream listing returned only rerankers).
    const noEmbJina = {
      ...EMBEDDING_ENDPOINT,
      role: "embedding" as const,
      models: ["jina-reranker-v2"],
      model_kinds: { "jina-reranker-v2": "chat" } as Record<string, "chat" | "embedding">,
    };
    vi.mocked(globalThis.fetch).mockImplementation(
      mkFetch({
        endpoints: [noEmbJina],
        assignment: mkAssignment("embedding", "ep-jina", "jina-embeddings-v4", 2048),
        state: { ...STATE_OK, persisted_provider: null, persisted_model: null },
      }),
    );
    renderTab();
    const endpointSelect = (await screen.findByLabelText("embedding endpoint")) as HTMLSelectElement;
    const labels = Array.from(endpointSelect.options).map((o) => o.textContent ?? "");
    expect(labels.some((l) => l.includes("(no embedding models — run Discover)"))).toBe(true);
  });

  it("shows the green 'up to date' pill only when the configured model == persisted", async () => {
    // Configured = persisted = jina_ai/jina-embeddings-v4 and migration_required=false.
    vi.mocked(globalThis.fetch).mockImplementation(
      mkFetch({
        assignment: mkAssignment("embedding", "ep-jina", "jina-embeddings-v4", 2048),
        state: {
          ...STATE_OK,
          migration_required: false,
          persisted_provider: "jina_ai",
          persisted_model: "jina-embeddings-v4",
          persisted_dimensions: 2048,
          fact_count: 1494,
        },
      }),
    );
    renderTab();
    await screen.findByLabelText("embedding endpoint");
    await waitFor(() => expect(screen.getByText(/Embeddings up to date/i)).toBeTruthy());
    expect(screen.getByText(/1,494 facts on/)).toBeTruthy();
    expect(screen.queryByText("Re-embed required")).toBeNull();
  });
});
