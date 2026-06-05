/**
 * SSE client for consuming the backend /api/channels/:id/ask stream.
 *
 * Reads the response body incrementally, dispatching events as they arrive
 * on `\n\n` boundaries so callers can observe response_delta tokens in real
 * time. Supports cancellation via AbortController and jittered exponential
 * backoff for retryable failures at the fetch layer.
 */

import type { AskResult, Citation, Tension } from "./types.js";

export interface SSEConsumeOptions {
  onDelta?: (delta: string) => void;
  signal?: AbortSignal;
}

interface SSEEvent {
  type: string;
  data: Record<string, unknown>;
}

/** Mutable accumulator shared across the SSE event loop. */
interface StreamState {
  answer: string;
  citations: Citation[];
  route: string;
  confidence: number;
  costUsd: number;
  lastSyncTs?: string;
  /** Backend-provided empty-retrieval signal, if the metadata event carried it. */
  backendEmpty?: boolean;
  /** Suggested related questions from the backend `follow_ups` event. */
  followUps: string[];
  /** Documented tensions from the backend `related_context` event. */
  tensions: Tension[];
}

/**
 * Answers at/above this length are treated as substantive and never collapsed
 * into the empty state, even if the backend flags empty retrieval — guards
 * against hiding a real (if uncited) answer.
 */
const SUBSTANTIVE_ANSWER_CHARS = 600;

/** Phrases the QA agent emits when retrieval found nothing (see backend prompt). */
const EMPTY_PATTERN =
  /no indexed (memories|facts|wiki)|hasn'?t been synced|not been synced|could not find any indexed|don'?t have (any )?(indexed )?(memories|facts|wiki)|no record of/i;

/**
 * Phrases that mark a coherent CONVERSATIONAL reply (greeting / identity /
 * "here's how to get started") that the agent returns WITHOUT retrieving
 * anything — so it carries no citations and `is_empty_retrieval=true`, yet must
 * NOT be replaced by the generic empty state. (Live-test bug: a friendly
 * "Hi! I'm Beever Atlas…" answer to "hi" was being swallowed.) Kept tight and
 * only consulted when EMPTY_PATTERN does NOT also match, so a genuinely empty
 * "I have no indexed memories, but I can help" can't slip through.
 */
export const GUIDANCE_PATTERN =
  /beever atlas|i'?m (your|a|the) .*(assistant|agent)|here'?s how|you can (sync|index|ask|invite)|try asking|i can (search|help|find|answer)|what would you like|ask me/i;

function asString(v: unknown): string | undefined {
  return typeof v === "string" && v.length > 0 ? v : undefined;
}

function asRecord(v: unknown): Record<string, unknown> {
  return typeof v === "object" && v !== null ? (v as Record<string, unknown>) : {};
}

/**
 * Normalize a `citations` event into {@link Citation}[], handling both the
 * legacy flat `items` shape ({type, text, author, channel, permalink}) and the
 * registry `sources` shape ({kind, title, excerpt, permalink, native{...}}).
 */
export function normalizeCitations(data: Record<string, unknown>): Citation[] {
  const out: Citation[] = [];

  const items = Array.isArray(data.items) ? data.items : [];
  for (const raw of items) {
    const it = asRecord(raw);
    const text = asString(it.text) ?? asString(it.title) ?? asString(it.excerpt);
    if (!text) continue;
    out.push({
      type: asString(it.type) ?? asString(it.kind) ?? "source",
      text,
      author: asString(it.author),
      url: asString(it.permalink) ?? asString(it.url),
      source: asString(it.channel) ?? asString(it.source),
    });
  }
  if (out.length > 0) return out;

  const sources = Array.isArray(data.sources) ? data.sources : [];
  for (const raw of sources) {
    const s = asRecord(raw);
    const native = asRecord(s.native);
    const text = asString(s.title) ?? asString(s.excerpt);
    if (!text) continue;
    out.push({
      type: asString(s.kind) ?? "source",
      text,
      author: asString(native.author),
      url: asString(s.permalink) ?? asString(native.permalink),
      source: asString(native.channel_name) ?? asString(native.channel),
    });
  }
  return out;
}

/**
 * Normalize the `related_context` event's `tensions` into {@link Tension}[].
 * Tolerant of backend field naming (title/topic/summary, detail/description).
 */
export function normalizeTensions(data: Record<string, unknown>): Tension[] {
  const raw = Array.isArray(data.tensions) ? data.tensions : [];
  const out: Tension[] = [];
  for (const item of raw) {
    const t = asRecord(item);
    const title = asString(t.title) ?? asString(t.topic) ?? asString(t.summary);
    if (!title) continue;
    // Avoid "X — X": only use summary as detail when it wasn't promoted to title.
    let detail = asString(t.detail) ?? asString(t.description);
    if (!detail && asString(t.summary) !== title) detail = asString(t.summary);
    out.push({ title, detail });
    if (out.length >= 3) break;
  }
  return out;
}

/** True when retrieval clearly found nothing: no citations AND empty-pattern text. */
export function detectEmptyRetrieval(answer: string, citations: Citation[]): boolean {
  if (citations.length > 0) return false;
  return EMPTY_PATTERN.test(answer);
}

/**
 * Decide the final empty-state, combining the backend signal with the client
 * heuristic safely. Render the empty state only when ALL hold:
 *  - no citations (a cited answer is never "empty"),
 *  - the answer is not substantive (a long real answer is never hidden), AND
 *  - the answer is either a definitive empty-retrieval phrase OR the backend
 *    flagged empty retrieval and the answer is NOT a coherent conversational
 *    reply.
 *
 * The guidance guard fixes a live-test bug: a friendly "Hi! I'm Beever Atlas…"
 * greeting (no retrieval → no citations → `is_empty_retrieval=true`) was being
 * swallowed into the generic empty state. EMPTY_PATTERN keeps priority so a
 * genuinely empty answer that also offers help can't slip through the guard.
 */
export function resolveIsEmpty(
  answer: string,
  citations: Citation[],
  backendEmpty: boolean | undefined,
): boolean {
  if (citations.length > 0) return false;
  if (answer.trim().length >= SUBSTANTIVE_ANSWER_CHARS) return false;
  // A definitive "nothing found" phrase is empty even if it also offers help.
  if (EMPTY_PATTERN.test(answer)) return true;
  // A coherent greeting / identity / how-to reply is a valid answer — show it
  // verbatim instead of collapsing it just because retrieval returned nothing.
  if (GUIDANCE_PATTERN.test(answer)) return false;
  return backendEmpty === true;
}

function finalizeResult(state: StreamState): AskResult {
  return {
    answer: state.answer,
    citations: state.citations,
    route: state.route,
    confidence: state.confidence,
    costUsd: state.costUsd,
    isEmpty: resolveIsEmpty(state.answer, state.citations, state.backendEmpty),
    lastSyncTs: state.lastSyncTs,
    followUps: state.followUps,
    tensions: state.tensions,
  };
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
  state: StreamState,
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
      state.citations = normalizeCitations(event.data);
      break;
    case "metadata":
      state.route = (event.data.route as string) || "echo";
      state.confidence = (event.data.confidence as number) || 0;
      state.costUsd = (event.data.cost_usd as number) || 0;
      // Optional, backward-compatible enrichments (absent on older backends).
      if (typeof event.data.is_empty_retrieval === "boolean") {
        state.backendEmpty = event.data.is_empty_retrieval;
      }
      if (typeof event.data.last_sync_ts === "string") {
        state.lastSyncTs = event.data.last_sync_ts;
      }
      break;
    case "follow_ups": {
      const suggestions = event.data.suggestions;
      if (Array.isArray(suggestions)) {
        state.followUps = suggestions
          .filter((s): s is string => typeof s === "string" && s.trim().length > 0)
          .slice(0, 3);
      }
      break;
    }
    case "related_context":
      state.tensions = normalizeTensions(event.data);
      break;
    case "error":
      throw new Error((event.data.message as string) || "Unknown backend error");
  }
}

export async function consumeSSEStream(
  response: Response,
  options: SSEConsumeOptions = {},
): Promise<AskResult> {
  const state: StreamState = {
    answer: "",
    citations: [],
    route: "echo",
    confidence: 0,
    costUsd: 0,
    followUps: [],
    tensions: [],
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
    return finalizeResult(state);
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

  return finalizeResult(state);
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

    let response: Response;
    try {
      response = await fetch(url, { ...init, signal: options.signal });
    } catch (err) {
      // Network / transport error — retryable.
      if ((err as { name?: string })?.name === "AbortError") throw err;
      lastErr = err;
      if (attempt === maxAttempts - 1) break;
      await sleep(backoffDelayMs(attempt));
      continue;
    }

    // 5xx — server-side, retryable with backoff.
    if (response.status >= 500) {
      lastErr = new Error(`Backend returned ${response.status}`);
      if (attempt === maxAttempts - 1) break;
      await sleep(backoffDelayMs(attempt));
      continue;
    }

    // 4xx — client error (bad request / auth / not found). Retrying won't help
    // and only burns the timeout budget, so surface it immediately.
    if (!response.ok) {
      throw new Error(`Backend returned ${response.status}: ${await response.text()}`);
    }

    // Success: stream-consumption failures (e.g. an SSE `error` event) are
    // terminal too — they reflect an agent error, not a transient blip.
    return await consumeSSEStream(response, options);
  }
  throw lastErr ?? new Error("fetchSSEWithRetry: exhausted retries");
}
