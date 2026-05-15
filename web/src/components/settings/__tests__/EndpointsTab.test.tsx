import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { EndpointsTab } from "../EndpointsTab";

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
  models: ["gpt-4o-mini", "gpt-4o"],
  rpm: 500,
  headers: {},
  tags: [],
  last_test_at: null,
  last_test_ok: null,
  last_test_error: null,
  created_at: "2026-05-12T00:00:00Z",
  updated_at: "2026-05-12T00:00:00Z",
};

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

function renderTab() {
  return render(
    <MemoryRouter>
      <EndpointsTab />
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

describe("EndpointsTab", () => {
  it("zero endpoints renders the empty-state CTA + preset chips", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockImplementation(async (input: any) => {
      const url = String(input);
      if (url.includes("/api/settings/endpoints")) return makeResponse({ endpoints: [] });
      if (url.includes("/api/settings/assignments"))
        return makeResponse({ assignments: [], default_consumers: [], capabilities: {} });
      return makeResponse({});
    });

    renderTab();

    await waitFor(() => expect(screen.getByText("No endpoints yet")).toBeTruthy());
    expect(screen.getByText(/…or apply a preset:/)).toBeTruthy();
    // Quick-start preset chips.
    expect(screen.getByText("Gemini balanced")).toBeTruthy();
    expect(screen.getByText("OpenAI quality")).toBeTruthy();
  });

  it("with endpoints renders an EndpointCard per endpoint", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockImplementation(async (input: any) => {
      const url = String(input);
      if (url.includes("/api/settings/endpoints")) return makeResponse({ endpoints: [ENDPOINT] });
      if (url.includes("/api/settings/assignments"))
        return makeResponse({
          assignments: [mkAssignment("qa_agent", "ep-1", "gpt-4o")],
          default_consumers: ["qa_agent"],
          capabilities: {},
        });
      return makeResponse({});
    });

    renderTab();

    await waitFor(() => expect(screen.getByText("OpenAI prod")).toBeTruthy());
    // Masked credential is rendered by the card.
    expect(screen.getByText("sk-p...1234")).toBeTruthy();
    // usedByCount — qa_agent points at ep-1 → demoted "used by 1 agent" line.
    expect(screen.getByText(/used by 1 agent/i)).toBeTruthy();
    // The old noise chips are gone.
    expect(screen.queryByText(/\bjobs\b/)).toBeNull();
    expect(screen.queryByText(/RPM/)).toBeNull();
    // Test / Discover / Delete buttons.
    expect(screen.getByText("Test")).toBeTruthy();
    expect(screen.getByText("Discover")).toBeTruthy();
    expect(screen.getByText("Delete")).toBeTruthy();
  });

  it("'Add endpoint' button opens the AddEndpointPanel modal", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockImplementation(async (input: any) => {
      const url = String(input);
      if (url.includes("/api/settings/endpoints")) return makeResponse({ endpoints: [ENDPOINT] });
      if (url.includes("/api/settings/assignments"))
        return makeResponse({ assignments: [], default_consumers: [], capabilities: {} });
      return makeResponse({});
    });

    renderTab();
    await waitFor(() => expect(screen.getByText("OpenAI prod")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: /Add endpoint/i }));

    // A modal dialog appears with the create form.
    await waitFor(() => expect(screen.getByRole("dialog")).toBeTruthy());
    expect(screen.getByText("Base URL")).toBeTruthy();
    expect(screen.getByText("API key")).toBeTruthy();
    expect(screen.getByText("Models")).toBeTruthy();
  });

  it("clicking Edit opens the editor prefilled; saving without touching the key PUTs without api_key", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    const putBodies: any[] = [];
    fetchMock.mockImplementation(async (input: any, init?: any) => {
      const url = String(input);
      if (url.includes("/api/settings/endpoints/ep-1") && init?.method === "PUT") {
        putBodies.push(JSON.parse(init.body as string));
        return makeResponse({ ...ENDPOINT, name: "OpenAI staging" });
      }
      if (url.includes("/api/settings/endpoints")) return makeResponse({ endpoints: [ENDPOINT] });
      if (url.includes("/api/settings/assignments"))
        return makeResponse({ assignments: [], default_consumers: [], capabilities: {} });
      return makeResponse({});
    });

    renderTab();
    await waitFor(() => expect(screen.getByText("OpenAI prod")).toBeTruthy());

    fireEvent.click(screen.getByText("Edit"));

    // Editor opens prefilled with the endpoint's name + models (chips).
    await waitFor(() => expect(screen.getByText("Edit endpoint")).toBeTruthy());
    const nameInput = screen.getByDisplayValue("OpenAI prod") as HTMLInputElement;
    expect(nameInput).toBeTruthy();
    // "gpt-4o-mini" renders as a model chip in the editor (and possibly on the card too).
    expect(screen.getAllByText("gpt-4o-mini").length).toBeGreaterThan(0);

    fireEvent.change(nameInput, { target: { value: "OpenAI staging" } });
    fireEvent.click(screen.getByText("Save changes"));

    await waitFor(() => expect(putBodies.length).toBe(1));
    expect(putBodies[0].name).toBe("OpenAI staging");
    expect("api_key" in putBodies[0]).toBe(false);
    // Editor closes on success.
    await waitFor(() => expect(screen.queryByText("Edit endpoint")).toBeNull());
  });

  it("delete returning endpoint_in_use_* surfaces a friendly message", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockImplementation(async (input: any, init?: any) => {
      const url = String(input);
      if (url.includes("/api/settings/endpoints/ep-1") && init?.method === "DELETE") {
        return makeResponse({ detail: { error: "endpoint_in_use_as_primary_or_fallback" } }, false, 409);
      }
      if (url.includes("/api/settings/endpoints")) return makeResponse({ endpoints: [ENDPOINT] });
      if (url.includes("/api/settings/assignments"))
        return makeResponse({
          assignments: [mkAssignment("qa_agent", "ep-1", "gpt-4o")],
          default_consumers: ["qa_agent"],
          capabilities: {},
        });
      return makeResponse({});
    });

    renderTab();
    await waitFor(() => expect(screen.getByText("OpenAI prod")).toBeTruthy());

    fireEvent.click(screen.getByText("Delete"));

    await waitFor(() => expect(screen.getByText(/in use by: qa_agent/i)).toBeTruthy());
  });

  it("PR-γ: clicking Promote on an advanced model PUTs the endpoint with the extended manually_kept array", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    const putBodies: any[] = [];
    const endpointWithAdvanced = {
      ...ENDPOINT,
      models: ["gpt-4o-mini", "gpt-4o"],
      model_kinds: {
        "gpt-4o-mini": "chat",
        "gpt-4o": "chat",
      },
      advanced_models: ["jina-reranker-v2", "dall-e-3"],
      manually_kept: [],
    };
    // Pre-populate the discover result by triggering a discover call first.
    // We model that by having discover return a by_kind shape.
    fetchMock.mockImplementation(async (input: any, init?: any) => {
      const url = String(input);
      if (url.includes("/api/settings/endpoints/ep-1") && init?.method === "PUT") {
        putBodies.push(JSON.parse(init.body as string));
        return makeResponse({ ...endpointWithAdvanced, manually_kept: ["jina-reranker-v2"] });
      }
      if (url.includes("/api/settings/endpoints/ep-1/discover") && init?.method === "POST") {
        return makeResponse({
          ok: true,
          models: ["gpt-4o-mini", "gpt-4o"],
          error: null,
          by_kind: { chat: ["gpt-4o-mini", "gpt-4o"], embedding: [] },
          dropped_breakdown: { reranker: 1, image_gen: 1 },
        });
      }
      if (url.includes("/api/settings/endpoints")) return makeResponse({ endpoints: [endpointWithAdvanced] });
      if (url.includes("/api/settings/assignments"))
        return makeResponse({ assignments: [], default_consumers: [], capabilities: {} });
      return makeResponse({});
    });

    renderTab();
    await waitFor(() => expect(screen.getByText("OpenAI prod")).toBeTruthy());

    // Trigger Discover to get the rich summary panel rendered.
    fireEvent.click(screen.getByText("Discover"));
    // Wait for the "Show advanced" toggle to appear in the rich summary.
    await waitFor(() => expect(screen.getByText(/Show advanced/)).toBeTruthy());

    // The first PUT is the models-list write from handleDiscover; ignore it.
    putBodies.length = 0;

    // Reveal the advanced chips + click Promote on the first one.
    fireEvent.click(screen.getByText(/Show advanced/));
    await waitFor(() => expect(screen.getByText("jina-reranker-v2")).toBeTruthy());
    fireEvent.click(screen.getByLabelText("Promote jina-reranker-v2 to the model list"));

    await waitFor(() => expect(putBodies.length).toBeGreaterThan(0));
    // The PUT carries the extended manually_kept array.
    const promotePut = putBodies[putBodies.length - 1];
    expect(promotePut.manually_kept).toEqual(["jina-reranker-v2"]);
  });

  it("adding a model via the card's add-input PUTs the endpoint with the extended model list", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    const putBodies: any[] = [];
    fetchMock.mockImplementation(async (input: any, init?: any) => {
      const url = String(input);
      if (url.includes("/api/settings/endpoints/ep-1") && init?.method === "PUT") {
        putBodies.push(JSON.parse(init.body as string));
        return makeResponse({ ...ENDPOINT, models: [...ENDPOINT.models, "gpt-4.1"] });
      }
      if (url.includes("/api/settings/endpoints")) return makeResponse({ endpoints: [ENDPOINT] });
      if (url.includes("/api/settings/assignments"))
        return makeResponse({ assignments: [], default_consumers: [], capabilities: {} });
      return makeResponse({});
    });

    renderTab();
    await waitFor(() => expect(screen.getByText("OpenAI prod")).toBeTruthy());

    // 2 models (<= 8) → the model list is expanded by default; the add-input is present.
    const addInput = screen.getByLabelText("Add a model") as HTMLInputElement;
    fireEvent.change(addInput, { target: { value: "gpt-4.1" } });
    fireEvent.keyDown(addInput, { key: "Enter" });

    await waitFor(() => expect(putBodies.length).toBe(1));
    expect(putBodies[0].models).toEqual(["gpt-4o-mini", "gpt-4o", "gpt-4.1"]);
  });
});
