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
    // usedByCount — qa_agent points at ep-1 → "1 jobs".
    expect(screen.getByText(/1 jobs/)).toBeTruthy();
    // Test / Discover / Delete buttons.
    expect(screen.getByText("Test")).toBeTruthy();
    expect(screen.getByText("Discover")).toBeTruthy();
    expect(screen.getByText("Delete")).toBeTruthy();
  });

  it("'Add endpoint' button reveals the AddEndpointPanel", async () => {
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

    await waitFor(() => expect(screen.getByText("Base URL")).toBeTruthy());
    expect(screen.getByText("API key")).toBeTruthy();
    expect(screen.getByText("Models")).toBeTruthy();
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
});
