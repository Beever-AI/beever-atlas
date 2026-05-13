import { describe, it, expect, vi, type Mock } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { EndpointCard } from "../EndpointCard";
import type { Endpoint } from "@/lib/aiSetup";

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

describe("EndpointCard", () => {
  it("renders name, family chip, masked credential, status pill and the host line", () => {
    render(
      <EndpointCard
        endpoint={makeEndpoint()}
        usedByCount={3}
        usedByConsumers={["qa_agent", "embedding"]}
        onTest={() => {}}
        onDiscover={() => {}}
        onDelete={() => {}}
      />,
    );
    expect(screen.getByText("OpenAI prod")).toBeTruthy();
    // Family chip from the preset identity (no longer the raw preset key).
    expect(screen.getByText("OpenAI")).toBeTruthy();
    expect(screen.getByText("sk-p...1234")).toBeTruthy();
    expect(screen.getByText("untested")).toBeTruthy();
    // Host (not the full URL) is shown.
    expect(screen.getByText("api.openai.com")).toBeTruthy();
    // The "used by N agents" demoted line.
    expect(screen.getByText(/used by 3 agents/i)).toBeTruthy();
  });

  it("does NOT render the old noise chips (jobs / RPM / #tag)", () => {
    render(
      <EndpointCard
        endpoint={makeEndpoint({ rpm: 500, tags: ["prod"] })}
        usedByCount={3}
        onTest={() => {}}
        onDiscover={() => {}}
        onDelete={() => {}}
      />,
    );
    expect(screen.queryByText(/\bjobs\b/)).toBeNull();
    expect(screen.queryByText(/RPM/)).toBeNull();
    expect(screen.queryByText("#prod")).toBeNull();
  });

  it("uses the preset's friendly label and an 'auto-detected' badge for an env-hydrated endpoint", () => {
    render(
      <EndpointCard
        endpoint={makeEndpoint({
          preset: "google_ai",
          name: "google_ai (from GOOGLE_API_KEY)",
          tags: ["migrated-from-env"],
          base_url: "https://generativelanguage.googleapis.com/v1beta/openai/",
        })}
        usedByCount={0}
        onTest={() => {}}
        onDiscover={() => {}}
        onDelete={() => {}}
      />,
    );
    // Clean title, not the noisy auto-generated name.
    expect(screen.getByText("Google AI (Gemini)")).toBeTruthy();
    expect(screen.queryByText("google_ai (from GOOGLE_API_KEY)")).toBeNull();
    // The discreet badge naming the env var.
    expect(screen.getByText(/auto-detected from/i)).toBeTruthy();
    expect(screen.getByText("GOOGLE_API_KEY")).toBeTruthy();
  });

  it("keeps an operator-set name as-is (no auto-detected badge)", () => {
    render(
      <EndpointCard
        endpoint={makeEndpoint({ name: "Prod Gemini", preset: "google_ai" })}
        usedByCount={0}
        onTest={() => {}}
        onDiscover={() => {}}
        onDelete={() => {}}
      />,
    );
    expect(screen.getByText("Prod Gemini")).toBeTruthy();
    expect(screen.queryByText(/auto-detected from/i)).toBeNull();
  });

  it("calls onTest when the Test button is clicked", () => {
    const onTest = vi.fn();
    render(
      <EndpointCard
        endpoint={makeEndpoint()}
        usedByCount={0}
        onTest={onTest}
        onDiscover={() => {}}
        onDelete={() => {}}
      />,
    );
    fireEvent.click(screen.getByText("Test"));
    expect(onTest).toHaveBeenCalledTimes(1);
  });

  it("shows the red inline error when a failing testResult is passed", () => {
    render(
      <EndpointCard
        endpoint={makeEndpoint()}
        usedByCount={0}
        testResult={{ ok: false, latency_ms: null, error: "401 Unauthorized" }}
        onTest={() => {}}
        onDiscover={() => {}}
        onDelete={() => {}}
      />,
    );
    expect(screen.getByText(/Test failed: 401 Unauthorized/)).toBeTruthy();
  });

  it("shows the green inline result when a passing testResult is passed", () => {
    render(
      <EndpointCard
        endpoint={makeEndpoint()}
        usedByCount={0}
        testResult={{ ok: true, latency_ms: 312, error: null }}
        onTest={() => {}}
        onDiscover={() => {}}
        onDelete={() => {}}
      />,
    );
    expect(screen.getByText(/Connected · 312ms/)).toBeTruthy();
  });

  it("shows the discover result line", () => {
    render(
      <EndpointCard
        endpoint={makeEndpoint()}
        usedByCount={0}
        discoverResult={{ ok: true, count: 14, error: null }}
        onTest={() => {}}
        onDiscover={() => {}}
        onDelete={() => {}}
      />,
    );
    expect(screen.getByText(/Discovered 14 models — added/)).toBeTruthy();
  });

  it("shows the 'no key' status when has_credential is false and auth_type is not none", () => {
    render(
      <EndpointCard
        endpoint={makeEndpoint({ has_credential: false, auth_type: "api_key" })}
        usedByCount={0}
        onTest={() => {}}
        onDiscover={() => {}}
        onDelete={() => {}}
      />,
    );
    expect(screen.getByText("no key")).toBeTruthy();
  });

  it("does not show 'no key' for a none-auth endpoint without a credential", () => {
    render(
      <EndpointCard
        endpoint={makeEndpoint({ has_credential: false, auth_type: "none", preset: "ollama" })}
        usedByCount={0}
        onTest={() => {}}
        onDiscover={() => {}}
        onDelete={() => {}}
      />,
    );
    expect(screen.queryByText("no key")).toBeNull();
    expect(screen.getByText("untested")).toBeTruthy();
  });

  it("renders an Edit button when onEdit is given and calls it on click", () => {
    const onEdit = vi.fn();
    render(
      <EndpointCard
        endpoint={makeEndpoint()}
        usedByCount={0}
        onEdit={onEdit}
        onTest={() => {}}
        onDiscover={() => {}}
        onDelete={() => {}}
      />,
    );
    fireEvent.click(screen.getByText("Edit"));
    expect(onEdit).toHaveBeenCalledTimes(1);
  });

  it("shows the base URL as a host and falls back to the raw string when it doesn't parse", () => {
    render(
      <EndpointCard
        endpoint={makeEndpoint({ preset: "ollama", auth_type: "none", base_url: "localhost:11434" })}
        usedByCount={0}
        onTest={() => {}}
        onDiscover={() => {}}
        onDelete={() => {}}
      />,
    );
    expect(screen.getByText("localhost:11434")).toBeTruthy();
  });

  it("shows '(no base URL — set in Edit)' when base_url is empty", () => {
    render(
      <EndpointCard
        endpoint={makeEndpoint({ preset: "custom", base_url: "" })}
        usedByCount={0}
        onTest={() => {}}
        onDiscover={() => {}}
        onDelete={() => {}}
      />,
    );
    expect(screen.getByText(/no base URL — set in Edit/i)).toBeTruthy();
  });

  it("copies the full base URL to the clipboard when the copy button is clicked", () => {
    const writeText = vi.fn();
    Object.assign(navigator, { clipboard: { writeText } });
    render(
      <EndpointCard
        endpoint={makeEndpoint()}
        usedByCount={0}
        onTest={() => {}}
        onDiscover={() => {}}
        onDelete={() => {}}
      />,
    );
    fireEvent.click(screen.getByLabelText("Copy endpoint URL"));
    expect(writeText).toHaveBeenCalledWith("https://api.openai.com/v1");
  });

  it("renders model chips expanded-by-default for short lists", () => {
    render(
      <EndpointCard
        endpoint={makeEndpoint({ models: ["gpt-4o-mini", "gpt-4o", "o4-mini"] })}
        usedByCount={0}
        onTest={() => {}}
        onDiscover={() => {}}
        onDelete={() => {}}
      />,
    );
    // <= 8 models → expanded without a click.
    expect(screen.getByText("gpt-4o-mini")).toBeTruthy();
    expect(screen.getByText("o4-mini")).toBeTruthy();
  });

  it("collapses long model lists (> 8) until the toggle is clicked", () => {
    const many = Array.from({ length: 10 }, (_, i) => `m-${i}`);
    render(
      <EndpointCard
        endpoint={makeEndpoint({ models: many })}
        usedByCount={0}
        onTest={() => {}}
        onDiscover={() => {}}
        onDelete={() => {}}
      />,
    );
    expect(screen.queryByText("m-9")).toBeNull();
    fireEvent.click(screen.getByText("10 models"));
    expect(screen.getByText("m-9")).toBeTruthy();
  });

  it("clicking a model chip's ✕ calls onUpdateModels without that model", async () => {
    const onUpdateModels: Mock<(models: string[]) => Promise<void>> = vi
      .fn<(models: string[]) => Promise<void>>()
      .mockResolvedValue(undefined);
    render(
      <EndpointCard
        endpoint={makeEndpoint({ models: ["gpt-4o-mini", "gpt-4o"] })}
        usedByCount={0}
        onTest={() => {}}
        onDiscover={() => {}}
        onDelete={() => {}}
        onUpdateModels={onUpdateModels}
      />,
    );
    fireEvent.click(screen.getByLabelText("Remove model gpt-4o-mini"));
    await waitFor(() => expect(onUpdateModels).toHaveBeenCalledTimes(1));
    expect(onUpdateModels.mock.calls[0][0]).toEqual(["gpt-4o"]);
  });

  it("typing in the add-model input and pressing Enter calls onUpdateModels with the extended list", async () => {
    const onUpdateModels: Mock<(models: string[]) => Promise<void>> = vi
      .fn<(models: string[]) => Promise<void>>()
      .mockResolvedValue(undefined);
    render(
      <EndpointCard
        endpoint={makeEndpoint({ models: ["gpt-4o-mini"] })}
        usedByCount={0}
        onTest={() => {}}
        onDiscover={() => {}}
        onDelete={() => {}}
        onUpdateModels={onUpdateModels}
      />,
    );
    const input = screen.getByLabelText("Add a model") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "gpt-4.1" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(onUpdateModels).toHaveBeenCalledTimes(1));
    expect(onUpdateModels.mock.calls[0][0]).toEqual(["gpt-4o-mini", "gpt-4.1"]);
  });

  it("shows '(no models — …)' and disables the model toggle when there are no models", () => {
    render(
      <EndpointCard
        endpoint={makeEndpoint({ models: [] })}
        usedByCount={0}
        onTest={() => {}}
        onDiscover={() => {}}
        onDelete={() => {}}
      />,
    );
    expect(screen.getByText("0 models")).toBeTruthy();
    expect((screen.getByText("0 models") as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/no models/i)).toBeTruthy();
  });

  // ── PR-γ: rich Discover summary + advanced promotion ────────────────────

  it("PR-γ: rich Discover summary renders kept/filtered counts and the chat/embedding split", () => {
    render(
      <EndpointCard
        endpoint={makeEndpoint({
          models: ["gpt-4o-mini", "gpt-4o", "gpt-4.1", "text-embedding-3-large", "text-embedding-3-small"],
          model_kinds: {
            "gpt-4o-mini": "chat",
            "gpt-4o": "chat",
            "gpt-4.1": "chat",
            "text-embedding-3-large": "embedding",
            "text-embedding-3-small": "embedding",
          },
        })}
        usedByCount={0}
        discoverResult={{
          ok: true,
          count: 5,
          error: null,
          by_kind: {
            chat: ["gpt-4o-mini", "gpt-4o", "gpt-4.1"],
            embedding: ["text-embedding-3-large", "text-embedding-3-small"],
          },
          dropped_breakdown: { reranker: 1, image_gen: 2 },
        }}
        onTest={() => {}}
        onDiscover={() => {}}
        onDelete={() => {}}
      />,
    );
    // "Discovered 5 models · 3 filtered" lives in the summary card.
    expect(screen.getByText(/Discovered 5 models/)).toBeTruthy();
    expect(screen.getByText(/3 filtered$/)).toBeTruthy();
    // Sub-line carries the chat/embedding split.
    expect(screen.getByText(/3 chat · 2 embedding · 3 filtered out/)).toBeTruthy();
    // The tooltip carries the breakdown.
    const info = screen.getByLabelText(/Breakdown: 1 reranker · 2 image gen/);
    expect(info).toBeTruthy();
    // The legacy "Discovered N models — added" line is NOT also rendered.
    expect(screen.queryByText(/Discovered 5 models — added/)).toBeNull();
  });

  it("PR-γ: [Show advanced] toggle reveals advanced_models chips and Promote calls onPromoteAdvanced", async () => {
    const onPromoteAdvanced: Mock<(model: string) => Promise<void>> = vi
      .fn<(model: string) => Promise<void>>()
      .mockResolvedValue(undefined);
    render(
      <EndpointCard
        endpoint={makeEndpoint({
          models: ["gpt-4o-mini"],
          model_kinds: { "gpt-4o-mini": "chat" },
          advanced_models: ["jina-reranker-v2", "dall-e-3"],
          manually_kept: [],
        })}
        usedByCount={0}
        discoverResult={{
          ok: true,
          count: 1,
          error: null,
          by_kind: { chat: ["gpt-4o-mini"], embedding: [] },
          dropped_breakdown: { reranker: 1, image_gen: 1 },
        }}
        onTest={() => {}}
        onDiscover={() => {}}
        onDelete={() => {}}
        onPromoteAdvanced={onPromoteAdvanced}
      />,
    );
    // The advanced panel is collapsed by default — chips not in the DOM yet.
    expect(screen.queryByText("jina-reranker-v2")).toBeNull();
    fireEvent.click(screen.getByText(/Show advanced/));
    expect(screen.getByText("jina-reranker-v2")).toBeTruthy();
    expect(screen.getByText("dall-e-3")).toBeTruthy();

    fireEvent.click(screen.getByLabelText("Promote jina-reranker-v2 to the model list"));
    await waitFor(() => expect(onPromoteAdvanced).toHaveBeenCalledTimes(1));
    expect(onPromoteAdvanced.mock.calls[0][0]).toBe("jina-reranker-v2");
  });

  it("PR-γ: pre-α endpoints (no by_kind) fall back to the legacy 'Discovered N models — added' line", () => {
    render(
      <EndpointCard
        endpoint={makeEndpoint()}
        usedByCount={0}
        discoverResult={{ ok: true, count: 14, error: null }}
        onTest={() => {}}
        onDiscover={() => {}}
        onDelete={() => {}}
      />,
    );
    // Legacy line renders…
    expect(screen.getByText(/Discovered 14 models — added/)).toBeTruthy();
    // …and the rich summary's chat/embedding sub-line does NOT.
    expect(screen.queryByText(/chat · 0 embedding/)).toBeNull();
    expect(screen.queryByText(/Show advanced/)).toBeNull();
  });

  it("PR-γ: a successful Test with probed_model surfaces it in the inline pass message", () => {
    render(
      <EndpointCard
        endpoint={makeEndpoint({ preset: "jina_ai" })}
        usedByCount={0}
        testResult={{
          ok: true,
          latency_ms: 187,
          error: null,
          probed_model: "jina-embeddings-v4",
          probed_kind: "embedding",
        }}
        onTest={() => {}}
        onDiscover={() => {}}
        onDelete={() => {}}
      />,
    );
    expect(screen.getByText(/Test passed \(probed jina-embeddings-v4, 187 ms\)/)).toBeTruthy();
  });
});
