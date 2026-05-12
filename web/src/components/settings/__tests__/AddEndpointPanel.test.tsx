import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { AddEndpointPanel } from "../AddEndpointPanel";

describe("AddEndpointPanel", () => {
  let onCreate: ReturnType<typeof vi.fn>;
  let onCancel: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    onCreate = vi.fn().mockResolvedValue(undefined);
    onCancel = vi.fn();
  });

  it("clicking a preset chip prefills name + base URL + models", () => {
    render(<AddEndpointPanel onCreate={onCreate} onCancel={onCancel} />);
    fireEvent.click(screen.getByText("Anthropic Claude"));

    const nameInput = screen.getByDisplayValue("Anthropic Claude") as HTMLInputElement;
    expect(nameInput.value).toBe("Anthropic Claude");
    const baseUrlInput = screen.getByDisplayValue("https://api.anthropic.com/v1") as HTMLInputElement;
    expect(baseUrlInput).toBeTruthy();
    // models prefilled, comma-separated
    expect(screen.getByDisplayValue(/claude-haiku-4-5/)).toBeTruthy();
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
});
