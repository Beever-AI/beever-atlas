import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
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
  it("renders name, preset, masked credential, status pill and body stats", () => {
    render(
      <EndpointCard
        endpoint={makeEndpoint()}
        usedByCount={3}
        onTest={() => {}}
        onDiscover={() => {}}
        onDelete={() => {}}
      />,
    );
    expect(screen.getByText("OpenAI prod")).toBeTruthy();
    expect(screen.getByText("openai")).toBeTruthy();
    expect(screen.getByText("sk-p...1234")).toBeTruthy();
    // untested status pill (no last_test, no testResult)
    expect(screen.getByText("untested")).toBeTruthy();
    // body stats
    expect(screen.getByText(/2 models/)).toBeTruthy();
    expect(screen.getByText(/3 jobs/)).toBeTruthy();
    expect(screen.getByText(/500 RPM/)).toBeTruthy();
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
});
