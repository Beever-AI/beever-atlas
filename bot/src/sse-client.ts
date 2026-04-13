/**
 * SSE client for consuming the backend /api/channels/:id/ask stream.
 *
 * Reads the response body incrementally, dispatching events as they arrive
 * on `\n\n` boundaries so callers can observe response_delta tokens in real
 * time. Supports cancellation via AbortController and jittered exponential
 * backoff for retryable failures at the fetch layer.
 */

import type { AskResult } from "./index.js";

export interface SSEConsumeOptions {
  onDelta?: (delta: string) => void;
  signal?: AbortSignal;
}

interface SSEEvent {
  type: string;
  data: Record<string, unknown>;
}

function parseSSEBlock(block: string): SSEEvent | null {
  let currentType = "";
  let dataPayload: Record<string, unknown> | null = null;
  for (const line of block.split("\n")) {
    if (line.startsWith("event: ")) {
      currentType = line.slice(7).trim();
    } else if (line.startsWith("data: ") && currentType) {
      try {
        dataPayload = JSON.parse(line.slice(6)) as Record<string, unknown>;
      } catch {
        dataPayload = null;
      }
    }
  }
  if (!currentType || dataPayload === null) return null;
  return { type: currentType, data: dataPayload };
}

function applyEvent(
  event: SSEEvent,
  state: { answer: string; citations: AskResult["citations"]; route: string; confidence: number; costUsd: number },
  onDelta?: (delta: string) => void,
): void {
  switch (event.type) {
    case "response_delta": {
      const delta = (event.data.delta as string) || "";
      state.answer += delta;
      if (delta && onDelta) onDelta(delta);
      break;
    }
    case "citations":
      state.citations = (event.data.items as AskResult["citations"]) || [];
      break;
    case "metadata":
      state.route = (event.data.route as string) || "echo";
      state.confidence = (event.data.confidence as number) || 0;
      state.costUsd = (event.data.cost_usd as number) || 0;
      break;
    case "error":
      throw new Error((event.data.message as string) || "Unknown backend error");
  }
}

export async function consumeSSEStream(
  response: Response,
  options: SSEConsumeOptions = {},
): Promise<AskResult> {
  const state = {
    answer: "",
    citations: [] as AskResult["citations"],
    route: "echo",
    confidence: 0,
    costUsd: 0,
  };

  if (!response.body) {
    const text = await response.text();
    let buf = text;
    let idx: number;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const event = parseSSEBlock(block);
      if (event) applyEvent(event, state, options.onDelta);
    }
    return state;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const onAbort = () => {
    reader.cancel().catch(() => {});
  };
  if (options.signal) {
    if (options.signal.aborted) {
      await reader.cancel().catch(() => {});
      throw new DOMException("Aborted", "AbortError");
    }
    options.signal.addEventListener("abort", onAbort, { once: true });
  }

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const event = parseSSEBlock(block);
        if (event) applyEvent(event, state, options.onDelta);
      }
    }
    buffer += decoder.decode();
    if (buffer.trim()) {
      const event = parseSSEBlock(buffer);
      if (event) applyEvent(event, state, options.onDelta);
    }
  } finally {
    if (options.signal) options.signal.removeEventListener("abort", onAbort);
  }

  return state;
}

/**
 * Jittered exponential backoff delay for retryable fetch failures.
 * delay = min(30000, 500 * 2**attempt) + random(0..250) ms.
 */
export function backoffDelayMs(attempt: number, rng: () => number = Math.random): number {
  const base = Math.min(30000, 500 * 2 ** attempt);
  return base + rng() * 250;
}

export interface FetchSSEOptions extends SSEConsumeOptions {
  maxAttempts?: number;
  sleep?: (ms: number) => Promise<void>;
}

/**
 * Fetch an SSE endpoint with jittered exponential backoff on 5xx / network
 * errors. Non-5xx HTTP errors and aborts are surfaced immediately.
 */
export async function fetchSSEWithRetry(
  url: string,
  init: RequestInit,
  options: FetchSSEOptions = {},
): Promise<AskResult> {
  const maxAttempts = options.maxAttempts ?? 4;
  const sleep = options.sleep ?? ((ms: number) => new Promise((r) => setTimeout(r, ms)));
  let lastErr: unknown = null;

  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    if (options.signal?.aborted) throw new DOMException("Aborted", "AbortError");
    try {
      const response = await fetch(url, { ...init, signal: options.signal });
      if (response.status >= 500) {
        lastErr = new Error(`Backend returned ${response.status}`);
        await sleep(backoffDelayMs(attempt));
        continue;
      }
      if (!response.ok) {
        throw new Error(`Backend returned ${response.status}: ${await response.text()}`);
      }
      return await consumeSSEStream(response, options);
    } catch (err) {
      if ((err as { name?: string })?.name === "AbortError") throw err;
      lastErr = err;
      if (attempt === maxAttempts - 1) break;
      await sleep(backoffDelayMs(attempt));
    }
  }
  throw lastErr ?? new Error("fetchSSEWithRetry: exhausted retries");
}
