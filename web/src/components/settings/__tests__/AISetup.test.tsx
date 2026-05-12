import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { AISetup } from "../AISetup";

function makeResponse(body: unknown, ok = true, status = 200): Response {
  return {
    ok,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

const EMPTY_ENDPOINTS = { endpoints: [] };
const EMPTY_ASSIGNMENTS = { assignments: [], default_consumers: [], capabilities: {} };

const ONE_ENDPOINT = {
  endpoints: [
    {
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
    },
  ],
};

const ASSIGNMENTS_WITH_QA = {
  assignments: [
    {
      consumer: "qa_agent",
      endpoint_id: "ep-1",
      model: "gpt-4o",
      temperature: null,
      max_tokens: null,
      response_format: null,
      extra_headers: {},
      fallback_endpoint_id: null,
      dimensions: null,
      task: null,
      updated_at: "2026-05-12T00:00:00Z",
    },
  ],
  default_consumers: ["qa_agent", "fact_extractor"],
  capabilities: { qa_agent: ["tools"], image_describer: ["vision"] },
};

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("AISetup", () => {
  it("renders the Quick start preset row + Endpoints section when empty", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockImplementation(async (input: any) => {
      const url = String(input);
      if (url.includes("/api/settings/endpoints")) return makeResponse(EMPTY_ENDPOINTS);
      if (url.includes("/api/settings/assignments")) return makeResponse(EMPTY_ASSIGNMENTS);
      return makeResponse({});
    });

    render(<AISetup />);

    await waitFor(() => {
      expect(screen.getByText("Quick start")).toBeTruthy();
    });
    // Preset chips present.
    expect(screen.getByText("Gemini balanced")).toBeTruthy();
    expect(screen.getByText("OpenAI quality")).toBeTruthy();
    expect(screen.getByText("Fully local (Ollama)")).toBeTruthy();
    // Endpoints section + empty-state copy.
    expect(screen.getByText("Endpoints")).toBeTruthy();
    expect(screen.getByText(/No endpoints configured yet/i)).toBeTruthy();
    // Add endpoint button.
    expect(screen.getByText("Add endpoint")).toBeTruthy();
  });

  it("shows the Add Endpoint form with preset chips when the button is clicked", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockImplementation(async (input: any) => {
      const url = String(input);
      if (url.includes("/api/settings/endpoints")) return makeResponse(EMPTY_ENDPOINTS);
      if (url.includes("/api/settings/assignments")) return makeResponse(EMPTY_ASSIGNMENTS);
      return makeResponse({});
    });

    render(<AISetup />);
    await waitFor(() => expect(screen.getByText("Add endpoint")).toBeTruthy());

    fireEvent.click(screen.getByText("Add endpoint"));

    // Form fields appear.
    await waitFor(() => expect(screen.getByText("Base URL")).toBeTruthy());
    expect(screen.getByText("API key")).toBeTruthy();
    expect(screen.getByText("Models")).toBeTruthy();
    // Anthropic preset chip is selectable in the form.
    expect(screen.getByText("Anthropic Claude")).toBeTruthy();
  });

  it("renders endpoint rows with status + masked credential + usage count", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockImplementation(async (input: any) => {
      const url = String(input);
      if (url.includes("/api/settings/endpoints")) return makeResponse(ONE_ENDPOINT);
      if (url.includes("/api/settings/assignments")) return makeResponse(ASSIGNMENTS_WITH_QA);
      return makeResponse({});
    });

    render(<AISetup />);

    // "OpenAI prod" appears both in the endpoint row AND in the assignment
    // endpoint <select> options, so query for all of them.
    await waitFor(() => expect(screen.getAllByText("OpenAI prod").length).toBeGreaterThan(0));
    // Masked credential shown (unique to the endpoint row).
    expect(screen.getByText("sk-p...1234")).toBeTruthy();
    // "· N jobs" — qa_agent uses ep-1.
    expect(screen.getByText(/\d+ jobs/)).toBeTruthy();
    // Test + Discover buttons present.
    expect(screen.getByText("Test")).toBeTruthy();
    expect(screen.getByText("Discover")).toBeTruthy();
  });

  it("renders the Assignments list grouped by category with capability badges", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockImplementation(async (input: any) => {
      const url = String(input);
      if (url.includes("/api/settings/endpoints")) return makeResponse(ONE_ENDPOINT);
      if (url.includes("/api/settings/assignments")) return makeResponse(ASSIGNMENTS_WITH_QA);
      return makeResponse({});
    });

    render(<AISetup />);

    await waitFor(() => expect(screen.getByText("Assignments")).toBeTruthy());
    // Group headers.
    expect(screen.getByText("Q&A agents")).toBeTruthy();
    expect(screen.getByText("Media agents")).toBeTruthy();
    // "Embedding" appears as both a group header AND in the legacy-tab note.
    expect(screen.getAllByText("Embedding").length).toBeGreaterThan(0);
    // qa_agent consumer row label.
    expect(screen.getByText("qa_agent")).toBeTruthy();
  });

  it("surfaces the legacy-tab pointer note", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockImplementation(async (input: any) => {
      const url = String(input);
      if (url.includes("/api/settings/endpoints")) return makeResponse(EMPTY_ENDPOINTS);
      if (url.includes("/api/settings/assignments")) return makeResponse(EMPTY_ASSIGNMENTS);
      return makeResponse({});
    });

    render(<AISetup />);
    await waitFor(() =>
      expect(screen.getByText(/legacy/i)).toBeTruthy()
    );
  });

  it("reveals the advanced-params drawer when the gear toggle is clicked", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockImplementation(async (input: any) => {
      const url = String(input);
      if (url.includes("/api/settings/endpoints")) return makeResponse(ONE_ENDPOINT);
      if (url.includes("/api/settings/assignments")) return makeResponse(ASSIGNMENTS_WITH_QA);
      return makeResponse({});
    });

    render(<AISetup />);
    // Wait for the qa_agent row to render.
    await waitFor(() => expect(screen.getByText("qa_agent")).toBeTruthy());

    // The advanced-params labels are not present until the gear is clicked.
    expect(screen.queryByText("temperature")).toBeNull();

    // The gear toggle has the title "Advanced parameters".
    const gear = screen.getByTitle("Advanced parameters");
    fireEvent.click(gear);

    // Drawer appears with the per-Assignment param fields.
    await waitFor(() => expect(screen.getByText("temperature")).toBeTruthy());
    expect(screen.getByText("max_tokens")).toBeTruthy();
    expect(screen.getByText("response_format")).toBeTruthy();
    expect(screen.getByText("fallback")).toBeTruthy();
  });
});
