import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { AgentAssignmentRow } from "../AgentAssignmentRow";
import type { Assignment, Endpoint } from "@/lib/aiSetup";

function makeEndpoint(overrides: Partial<Endpoint> = {}): Endpoint {
  return {
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
    ...overrides,
  };
}

function makeAssignment(overrides: Partial<Assignment> = {}): Assignment {
  return {
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
    ...overrides,
  };
}

const ep = makeEndpoint();

describe("AgentAssignmentRow", () => {
  it("renders displayName + description + provider badge", () => {
    render(
      <AgentAssignmentRow
        consumer="qa_agent"
        assignment={makeAssignment()}
        endpoints={[ep]}
        required={["tools"]}
        onUpsert={async () => makeAssignment()}
      />,
    );
    expect(screen.getByText("QA Agent")).toBeTruthy();
    expect(screen.getByText(/Answers user questions/i)).toBeTruthy();
    expect(screen.getByText("Cloud")).toBeTruthy();
  });

  it("changing the model select calls onUpsert with {endpoint_id, model}", async () => {
    const onUpsert = vi.fn(async () => makeAssignment({ model: "gpt-4o-mini" }));
    render(
      <AgentAssignmentRow
        consumer="qa_agent"
        assignment={makeAssignment()}
        endpoints={[ep]}
        required={["tools"]}
        onUpsert={onUpsert}
      />,
    );
    fireEvent.change(screen.getByLabelText("qa_agent model"), { target: { value: "gpt-4o-mini" } });
    await waitFor(() => expect(onUpsert).toHaveBeenCalled());
    expect(onUpsert).toHaveBeenCalledWith("qa_agent", { endpoint_id: "ep-1", model: "gpt-4o-mini" });
  });

  it("the Advanced gear reveals temperature/max_tokens/response_format/fallback", () => {
    render(
      <AgentAssignmentRow
        consumer="qa_agent"
        assignment={makeAssignment()}
        endpoints={[ep, makeEndpoint({ id: "ep-2", name: "Gemini" })]}
        required={[]}
        onUpsert={async () => makeAssignment()}
      />,
    );
    expect(screen.queryByText("temperature")).toBeNull();
    fireEvent.click(screen.getByTitle("Advanced parameters"));
    expect(screen.getByText("temperature")).toBeTruthy();
    expect(screen.getByText("max_tokens")).toBeTruthy();
    expect(screen.getByText("response_format")).toBeTruthy();
    expect(screen.getByText("fallback")).toBeTruthy();
  });

  it("Reset calls onUpsert with the params nulled (keeping endpoint+model)", async () => {
    const onUpsert = vi.fn(async () => makeAssignment());
    render(
      <AgentAssignmentRow
        consumer="qa_agent"
        assignment={makeAssignment({ temperature: 0.3, max_tokens: 1024 })}
        endpoints={[ep]}
        required={[]}
        onUpsert={onUpsert}
      />,
    );
    // The reset button has the title "Reset advanced overrides".
    fireEvent.click(screen.getByTitle("Reset advanced overrides"));
    await waitFor(() => expect(onUpsert).toHaveBeenCalled());
    expect(onUpsert).toHaveBeenCalledWith("qa_agent", {
      endpoint_id: "ep-1",
      model: "gpt-4o",
      temperature: null,
      max_tokens: null,
      response_format: null,
      fallback_endpoint_id: null,
    });
  });

  it("a row whose model is incompatible shows the red capability badge", () => {
    const { container } = render(
      <AgentAssignmentRow
        consumer="image_describer"
        assignment={makeAssignment({ consumer: "image_describer", model: "o4-mini" })}
        endpoints={[ep]}
        required={["vision"]}
        onUpsert={async () => makeAssignment()}
      />,
    );
    const badge = container.querySelector('[data-capability="vision"][data-incompatible="true"]');
    expect(badge).not.toBeNull();
  });

  it("PR-γ: filters the Model select to only chat-classified models from endpoint.model_kinds", () => {
    const mixed = makeEndpoint({
      models: ["gpt-4o-mini", "gpt-4o", "text-embedding-3-large"],
      model_kinds: {
        "gpt-4o-mini": "chat",
        "gpt-4o": "chat",
        "text-embedding-3-large": "embedding",
      },
    });
    render(
      <AgentAssignmentRow
        consumer="qa_agent"
        assignment={makeAssignment({ endpoint_id: "ep-1", model: "gpt-4o" })}
        endpoints={[mixed]}
        required={["tools"]}
        onUpsert={async () => makeAssignment()}
      />,
    );
    const modelSelect = screen.getByLabelText("qa_agent model") as HTMLSelectElement;
    const optionValues = Array.from(modelSelect.options).map((o) => o.value);
    // Chat-classified models only…
    expect(optionValues).toContain("gpt-4o-mini");
    expect(optionValues).toContain("gpt-4o");
    // …embedding model is filtered out.
    expect(optionValues).not.toContain("text-embedding-3-large");
  });

  it("PR-γ: pre-α endpoints (empty model_kinds) keep showing every endpoint.models[] entry", () => {
    const preAlpha = makeEndpoint({
      models: ["gpt-4o-mini", "gpt-4o", "text-embedding-3-large"],
      // No model_kinds set.
    });
    render(
      <AgentAssignmentRow
        consumer="qa_agent"
        assignment={makeAssignment({ endpoint_id: "ep-1", model: "gpt-4o" })}
        endpoints={[preAlpha]}
        required={["tools"]}
        onUpsert={async () => makeAssignment()}
      />,
    );
    const modelSelect = screen.getByLabelText("qa_agent model") as HTMLSelectElement;
    const optionValues = Array.from(modelSelect.options).map((o) => o.value);
    // Fallback: every model is offered (capability gate still narrows further).
    expect(optionValues).toContain("gpt-4o-mini");
    expect(optionValues).toContain("gpt-4o");
    expect(optionValues).toContain("text-embedding-3-large");
  });

  it("PR-ι: classifier produced zero chat models → endpoint is hidden from the chat-agent dropdown", () => {
    const noChat = makeEndpoint({
      id: "ep-embed",
      name: "Embed only",
      models: ["text-embedding-3-large"],
      model_kinds: { "text-embedding-3-large": "embedding" },
    });
    const chatOk = makeEndpoint({ id: "ep-chat", name: "Chat ok" });
    render(
      <AgentAssignmentRow
        consumer="qa_agent"
        assignment={undefined}
        endpoints={[noChat, chatOk]}
        required={[]}
        onUpsert={async () => makeAssignment()}
      />,
    );
    const endpointSelect = screen.getByLabelText("qa_agent endpoint") as HTMLSelectElement;
    const labels = Array.from(endpointSelect.options).map((o) => o.textContent ?? "");
    // The embedding-only endpoint is GONE from the dropdown (not labelled).
    expect(labels.some((l) => l.includes("Embed only"))).toBe(false);
    expect(labels.some((l) => l.includes("Chat ok"))).toBe(true);
  });

  it("PR-ι: jina_ai / voyage presets are hidden from chat-agent dropdown by preset alone", () => {
    const jina = makeEndpoint({ id: "ep-jina", name: "Jina AI", preset: "jina_ai" });
    const voyage = makeEndpoint({ id: "ep-voyage", name: "Voyage", preset: "voyage" });
    const openai = makeEndpoint({ id: "ep-openai", name: "OpenAI", preset: "openai" });
    render(
      <AgentAssignmentRow
        consumer="qa_agent"
        assignment={undefined}
        endpoints={[jina, voyage, openai]}
        required={[]}
        onUpsert={async () => makeAssignment()}
      />,
    );
    const endpointSelect = screen.getByLabelText("qa_agent endpoint") as HTMLSelectElement;
    const labels = Array.from(endpointSelect.options).map((o) => o.textContent ?? "");
    expect(labels.some((l) => l.includes("Jina AI"))).toBe(false);
    expect(labels.some((l) => l.includes("Voyage"))).toBe(false);
    expect(labels.some((l) => l.includes("OpenAI"))).toBe(true);
  });

  it("PR-ι: endpoints with role='embedding' (operator-declared) are also hidden", () => {
    const embedRole = makeEndpoint({
      id: "ep-er",
      name: "Custom embedding",
      preset: "openai",
      role: "embedding",
    });
    const chatOk = makeEndpoint({ id: "ep-chat", name: "Chat ok", preset: "openai", role: "chat" });
    render(
      <AgentAssignmentRow
        consumer="qa_agent"
        assignment={undefined}
        endpoints={[embedRole, chatOk]}
        required={[]}
        onUpsert={async () => makeAssignment()}
      />,
    );
    const endpointSelect = screen.getByLabelText("qa_agent endpoint") as HTMLSelectElement;
    const labels = Array.from(endpointSelect.options).map((o) => o.textContent ?? "");
    expect(labels.some((l) => l.includes("Custom embedding"))).toBe(false);
    expect(labels.some((l) => l.includes("Chat ok"))).toBe(true);
  });

  // PR-μ: removed the "Use {suggested}" button + red banner. With
  // saves now sent as ``force: true`` (PR-λ.6), the backend doesn't
  // return ``suggested`` anymore, and gating operators by a
  // substring-based capability classifier created more false-positive
  // lockouts than it prevented bad picks. Truth is now the runtime
  // "Last call" indicator (PR-λ.2).
  it("PR-μ: model dropdown shows every chat model without disabling on capability", () => {
    render(
      <AgentAssignmentRow
        consumer="qa_agent"
        assignment={makeAssignment({ endpoint_id: "ep-1", model: "gpt-4o-mini" })}
        endpoints={[ep]}
        required={["tools"]}
        onUpsert={async () => makeAssignment()}
      />,
    );
    const modelSelect = screen.getByLabelText("qa_agent model") as HTMLSelectElement;
    const options = Array.from(modelSelect.options);
    // No "(incompatible)" suffix anywhere.
    expect(options.some((o) => /incompatible/i.test(o.text))).toBe(false);
    // No disabled options (all picks land at runtime, not pre-save).
    expect(options.some((o) => o.disabled)).toBe(false);
  });
});
