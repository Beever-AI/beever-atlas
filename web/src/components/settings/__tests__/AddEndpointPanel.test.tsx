import { describe, it, expect, vi, beforeEach, type Mock } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { AddEndpointPanel } from "../AddEndpointPanel";
import type { CreateEndpointRequest, Endpoint, UpdateEndpointRequest } from "@/lib/aiSetup";

function makeEndpoint(overrides: Partial<Endpoint> = {}): Endpoint {
  return {
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
    ...overrides,
  };
}

describe("AddEndpointPanel", () => {
  let onCreate: Mock<(req: CreateEndpointRequest) => Promise<void>>;
  let onCancel: Mock<() => void>;

  beforeEach(() => {
    onCreate = vi.fn<(req: CreateEndpointRequest) => Promise<void>>().mockResolvedValue(undefined);
    onCancel = vi.fn<() => void>();
  });

  it("renders as a modal — backdrop + dialog role — and Escape closes it via onCancel", () => {
    render(<AddEndpointPanel onCreate={onCreate} onCancel={onCancel} />);
    // Dialog chrome present.
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.getByLabelText("Close")).toBeTruthy();
    // Escape routes to onCancel.
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("clicking the ✕ close button calls onCancel", () => {
    render(<AddEndpointPanel onCreate={onCreate} onCancel={onCancel} />);
    fireEvent.click(screen.getByLabelText("Close"));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("clicking a preset chip prefills name + base URL + models", () => {
    render(<AddEndpointPanel onCreate={onCreate} onCancel={onCancel} />);
    fireEvent.click(screen.getByText("Anthropic Claude"));

    const nameInput = screen.getByDisplayValue("Anthropic Claude") as HTMLInputElement;
    expect(nameInput.value).toBe("Anthropic Claude");
    const baseUrlInput = screen.getByDisplayValue("https://api.anthropic.com/v1") as HTMLInputElement;
    expect(baseUrlInput).toBeTruthy();
    // models prefilled as chips
    expect(screen.getByText("claude-haiku-4-5")).toBeTruthy();
    expect(screen.getByText("claude-sonnet-4-6")).toBeTruthy();
  });

  it("submitting calls onCreate with the right shape", async () => {
    render(<AddEndpointPanel onCreate={onCreate} onCancel={onCancel} initialPresetKey="openai" />);

    const apiKeyInput = screen.getByPlaceholderText("sk-...");
    fireEvent.change(apiKeyInput, { target: { value: "sk-test-123" } });
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => expect(onCreate).toHaveBeenCalledTimes(1));
    const req = onCreate.mock.calls[0][0];
    expect(req.name).toBe("OpenAI");
    expect(req.preset).toBe("openai");
    expect(req.base_url).toBe("https://api.openai.com/v1");
    expect(req.auth_type).toBe("api_key");
    expect(req.api_key).toBe("sk-test-123");
    expect(req.models).toEqual(["gpt-4o-mini", "gpt-4o", "gpt-4.1", "o4-mini"]);
  });

  it("models can be added and removed as chips, reflected in the request", async () => {
    render(<AddEndpointPanel onCreate={onCreate} onCancel={onCancel} initialPresetKey="openai" />);
    // openai default models present as chips
    expect(screen.getByText("gpt-4o-mini")).toBeTruthy();
    // remove one
    fireEvent.click(screen.getByLabelText("remove model gpt-4o-mini"));
    expect(screen.queryByText("gpt-4o-mini")).toBeNull();
    // add one (via Enter on the model input)
    const modelInput = screen.getByLabelText("add a model");
    fireEvent.change(modelInput, { target: { value: "gpt-4o-2025" } });
    fireEvent.keyDown(modelInput, { key: "Enter" });
    expect(screen.getByText("gpt-4o-2025")).toBeTruthy();
    // save → request reflects the edit
    fireEvent.change(screen.getByPlaceholderText("sk-..."), { target: { value: "sk-x" } });
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(onCreate).toHaveBeenCalledTimes(1));
    expect(onCreate.mock.calls[0][0].models).toEqual(["gpt-4o", "gpt-4.1", "o4-mini", "gpt-4o-2025"]);
  });

  it("renders the docs_url 'Get an API key' link", () => {
    render(<AddEndpointPanel onCreate={onCreate} onCancel={onCancel} initialPresetKey="openai" />);
    const link = screen.getByText(/Get an API key/).closest("a") as HTMLAnchorElement;
    expect(link).toBeTruthy();
    expect(link.href).toContain("platform.openai.com/api-keys");
  });

  it("hides the API key field for a none-auth preset (Ollama)", () => {
    render(<AddEndpointPanel onCreate={onCreate} onCancel={onCancel} initialPresetKey="ollama" />);
    expect(screen.queryByText("API key")).toBeNull();
    expect(screen.queryByPlaceholderText("sk-...")).toBeNull();
    // and onCreate omits api_key for none-auth
    fireEvent.click(screen.getByText("Save"));
    return waitFor(() => {
      expect(onCreate).toHaveBeenCalledTimes(1);
      const req = onCreate.mock.calls[0][0];
      expect(req.auth_type).toBe("none");
      expect(req.api_key).toBeUndefined();
    });
  });

  it("calls onCancel when Cancel is clicked", () => {
    render(<AddEndpointPanel onCreate={onCreate} onCancel={onCancel} />);
    fireEvent.click(screen.getByText("Cancel"));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("the Advanced section exposes RPM, headers and tags", () => {
    render(<AddEndpointPanel onCreate={onCreate} onCancel={onCancel} initialPresetKey="openai" />);
    // Collapsed by default — the RPM label isn't visible yet.
    expect(screen.queryByText("Rate limit (RPM)")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Advanced/i }));
    expect(screen.getByText("Rate limit (RPM)")).toBeTruthy();
    expect(screen.getByText("Extra headers")).toBeTruthy();
    expect(screen.getByText("Tags")).toBeTruthy();
  });

  it("Advanced RPM + tags flow through to the create request", async () => {
    render(<AddEndpointPanel onCreate={onCreate} onCancel={onCancel} initialPresetKey="ollama" />);
    fireEvent.click(screen.getByRole("button", { name: /Advanced/i }));
    fireEvent.change(screen.getByPlaceholderText("60"), { target: { value: "120" } });
    fireEvent.change(screen.getByPlaceholderText("comma-separated tags"), { target: { value: "prod, eu" } });
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(onCreate).toHaveBeenCalledTimes(1));
    const req = onCreate.mock.calls[0][0];
    expect(req.rpm).toBe(120);
    expect(req.tags).toEqual(["prod", "eu"]);
  });

  describe("edit mode", () => {
    let onUpdate: Mock<(req: UpdateEndpointRequest) => Promise<void>>;
    beforeEach(() => {
      onUpdate = vi.fn<(req: UpdateEndpointRequest) => Promise<void>>().mockResolvedValue(undefined);
    });

    it("prefills name + base URL + models from the existing endpoint and locks the preset", () => {
      render(<AddEndpointPanel mode="edit" existing={makeEndpoint()} onUpdate={onUpdate} onCancel={onCancel} />);
      expect(screen.getByText("Edit endpoint")).toBeTruthy();
      expect((screen.getByDisplayValue("OpenAI prod") as HTMLInputElement).value).toBe("OpenAI prod");
      expect(screen.getByDisplayValue("https://api.openai.com/v1")).toBeTruthy();
      // models render as chips
      expect(screen.getByText("gpt-4o-mini")).toBeTruthy();
      expect(screen.getByText("gpt-4o")).toBeTruthy();
      // No preset chips in edit mode (the preset is shown as a read-only label).
      expect(screen.queryByText("Anthropic Claude")).toBeNull();
    });

    it("saving without touching the key omits api_key from the request", async () => {
      render(<AddEndpointPanel mode="edit" existing={makeEndpoint()} onUpdate={onUpdate} onCancel={onCancel} />);
      fireEvent.click(screen.getByText("Save changes"));
      await waitFor(() => expect(onUpdate).toHaveBeenCalledTimes(1));
      const req = onUpdate.mock.calls[0][0];
      expect(req.name).toBe("OpenAI prod");
      expect(req.base_url).toBe("https://api.openai.com/v1");
      expect("api_key" in req).toBe(false);
    });

    it("'Replace key' reveals an input and then api_key is included", async () => {
      render(<AddEndpointPanel mode="edit" existing={makeEndpoint()} onUpdate={onUpdate} onCancel={onCancel} />);
      // Masked credential shown, no password input yet.
      expect(screen.getByText("sk-p...1234")).toBeTruthy();
      fireEvent.click(screen.getByText("Replace key"));
      const keyInput = screen.getByPlaceholderText("enter a new key…") as HTMLInputElement;
      fireEvent.change(keyInput, { target: { value: "sk-new-9999" } });
      fireEvent.click(screen.getByText("Save changes"));
      await waitFor(() => expect(onUpdate).toHaveBeenCalledTimes(1));
      expect(onUpdate.mock.calls[0][0].api_key).toBe("sk-new-9999");
    });

    it("prefills RPM / headers / tags from the existing endpoint in Advanced", () => {
      render(
        <AddEndpointPanel
          mode="edit"
          existing={makeEndpoint({ rpm: 90, headers: { "X-Org": "acme" }, tags: ["prod"] })}
          onUpdate={onUpdate}
          onCancel={onCancel}
        />,
      );
      fireEvent.click(screen.getByRole("button", { name: /Advanced/i }));
      expect(screen.getByDisplayValue("90")).toBeTruthy();
      expect(screen.getByDisplayValue("X-Org")).toBeTruthy();
      expect(screen.getByDisplayValue("acme")).toBeTruthy();
      expect(screen.getByDisplayValue("prod")).toBeTruthy();
    });
  });

  // ── PR-β: role radio ───────────────────────────────────────────────
  describe("role radio (PR-β)", () => {
    it("renders the role radio for ambiguous presets (openai)", () => {
      render(<AddEndpointPanel onCreate={onCreate} onCancel={onCancel} initialPresetKey="openai" />);
      expect(screen.getByText("What will you use this endpoint for?")).toBeTruthy();
      expect(screen.getByRole("radiogroup", { name: /endpoint role/i })).toBeTruthy();
      // All three choices visible.
      expect(screen.getByLabelText("Both (default)")).toBeTruthy();
      expect(screen.getByLabelText("Chat agents")).toBeTruthy();
      expect(screen.getByLabelText("Embeddings")).toBeTruthy();
    });

    it("hides the role radio for embedding-only presets (jina_ai)", () => {
      render(<AddEndpointPanel onCreate={onCreate} onCancel={onCancel} initialPresetKey="jina_ai" />);
      expect(screen.queryByText("What will you use this endpoint for?")).toBeNull();
    });

    it("hides the role radio for embedding-only preset (voyage)", () => {
      render(<AddEndpointPanel onCreate={onCreate} onCancel={onCancel} initialPresetKey="voyage" />);
      expect(screen.queryByText("What will you use this endpoint for?")).toBeNull();
    });

    it("hides the role radio for chat-only presets (anthropic, mistral)", () => {
      const { unmount } = render(
        <AddEndpointPanel onCreate={onCreate} onCancel={onCancel} initialPresetKey="anthropic" />,
      );
      expect(screen.queryByText("What will you use this endpoint for?")).toBeNull();
      unmount();
      render(<AddEndpointPanel onCreate={onCreate} onCancel={onCancel} initialPresetKey="mistral" />);
      expect(screen.queryByText("What will you use this endpoint for?")).toBeNull();
    });

    it("defaults role to 'both' for ambiguous presets and persists on submit", async () => {
      render(<AddEndpointPanel onCreate={onCreate} onCancel={onCancel} initialPresetKey="openai" />);
      fireEvent.change(screen.getByPlaceholderText("sk-..."), { target: { value: "sk-x" } });
      fireEvent.click(screen.getByText("Save"));
      await waitFor(() => expect(onCreate).toHaveBeenCalledTimes(1));
      expect(onCreate.mock.calls[0][0].role).toBe("both");
    });

    it("persists the selected role on submit (chat → 'chat')", async () => {
      render(<AddEndpointPanel onCreate={onCreate} onCancel={onCancel} initialPresetKey="openai" />);
      fireEvent.click(screen.getByLabelText("Chat agents"));
      fireEvent.change(screen.getByPlaceholderText("sk-..."), { target: { value: "sk-x" } });
      fireEvent.click(screen.getByText("Save"));
      await waitFor(() => expect(onCreate).toHaveBeenCalledTimes(1));
      expect(onCreate.mock.calls[0][0].role).toBe("chat");
    });

    it("persists the selected role on submit (embedding → 'embedding')", async () => {
      render(<AddEndpointPanel onCreate={onCreate} onCancel={onCancel} initialPresetKey="openai" />);
      fireEvent.click(screen.getByLabelText("Embeddings"));
      fireEvent.change(screen.getByPlaceholderText("sk-..."), { target: { value: "sk-x" } });
      fireEvent.click(screen.getByText("Save"));
      await waitFor(() => expect(onCreate).toHaveBeenCalledTimes(1));
      expect(onCreate.mock.calls[0][0].role).toBe("embedding");
    });

    it("seeds 'embedding' for embedding-only preset (jina_ai) even though the radio is hidden", async () => {
      render(<AddEndpointPanel onCreate={onCreate} onCancel={onCancel} initialPresetKey="jina_ai" />);
      fireEvent.change(screen.getByPlaceholderText("sk-..."), { target: { value: "k" } });
      fireEvent.click(screen.getByText("Save"));
      await waitFor(() => expect(onCreate).toHaveBeenCalledTimes(1));
      expect(onCreate.mock.calls[0][0].role).toBe("embedding");
    });

    it("seeds 'chat' for chat-only preset (anthropic) even though the radio is hidden", async () => {
      render(<AddEndpointPanel onCreate={onCreate} onCancel={onCancel} initialPresetKey="anthropic" />);
      fireEvent.change(screen.getByPlaceholderText("sk-..."), { target: { value: "k" } });
      fireEvent.click(screen.getByText("Save"));
      await waitFor(() => expect(onCreate).toHaveBeenCalledTimes(1));
      expect(onCreate.mock.calls[0][0].role).toBe("chat");
    });

    it("edit mode prefills the radio from existing.role", () => {
      const onUpdate = vi.fn<(req: UpdateEndpointRequest) => Promise<void>>().mockResolvedValue(undefined);
      render(
        <AddEndpointPanel
          mode="edit"
          existing={makeEndpoint({ role: "embedding" })}
          onUpdate={onUpdate}
          onCancel={onCancel}
        />,
      );
      // openai preset → radio visible; "Embeddings" should be checked.
      const radio = screen.getByLabelText("Embeddings") as HTMLInputElement;
      expect(radio.checked).toBe(true);
    });
  });
});
