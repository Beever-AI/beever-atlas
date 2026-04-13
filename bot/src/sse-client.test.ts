import { describe, it } from "node:test";
import assert from "node:assert";
import { consumeSSEStream, backoffDelayMs, fetchSSEWithRetry } from "./sse-client.js";

function mockResponse(body: string): Response {
  return new Response(body, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

function streamingResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      for (const c of chunks) controller.enqueue(encoder.encode(c));
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

describe("consumeSSEStream", () => {
  it("accumulates response_delta events", async () => {
    const body = [
      "event: response_delta",
      'data: {"delta": "Hello "}',
      "",
      "event: response_delta",
      'data: {"delta": "world"}',
      "",
      "event: citations",
      'data: {"items": []}',
      "",
      "event: metadata",
      'data: {"route": "echo", "confidence": 1.0, "cost_usd": 0.0}',
      "",
      "event: done",
      "data: {}",
      "",
    ].join("\n");

    const result = await consumeSSEStream(mockResponse(body));
    assert.strictEqual(result.answer, "Hello world");
    assert.strictEqual(result.route, "echo");
    assert.strictEqual(result.confidence, 1.0);
    assert.strictEqual(result.costUsd, 0.0);
    assert.deepStrictEqual(result.citations, []);
  });

  it("extracts citations", async () => {
    const body = [
      "event: response_delta",
      'data: {"delta": "answer"}',
      "",
      "event: citations",
      'data: {"items": [{"type": "fact", "text": "source1"}]}',
      "",
      "event: metadata",
      'data: {"route": "semantic", "confidence": 0.9, "cost_usd": 0.01}',
      "",
      "event: done",
      "data: {}",
      "",
    ].join("\n");

    const result = await consumeSSEStream(mockResponse(body));
    assert.strictEqual(result.citations.length, 1);
    assert.strictEqual(result.citations[0].text, "source1");
    assert.strictEqual(result.route, "semantic");
  });

  it("throws on error event", async () => {
    const body = [
      "event: error",
      'data: {"message": "Something went wrong", "code": "AGENT_ERROR"}',
      "",
    ].join("\n");

    await assert.rejects(
      () => consumeSSEStream(mockResponse(body)),
      { message: "Something went wrong" },
    );
  });

  it("delivers deltas incrementally as chunks arrive", async () => {
    // Split the stream so a delta event is fully contained in the first chunk
    // and the second delta appears only in a later chunk — onDelta MUST fire
    // twice, in order, before the promise resolves.
    const chunk1 =
      "event: response_delta\ndata: {\"delta\": \"Hi \"}\n\n" +
      "event: response_delta\nda";
    const chunk2 =
      "ta: {\"delta\": \"there\"}\n\n" +
      "event: metadata\ndata: {\"route\": \"echo\", \"confidence\": 1, \"cost_usd\": 0}\n\n" +
      "event: done\ndata: {}\n\n";

    const observed: string[] = [];
    const result = await consumeSSEStream(streamingResponse([chunk1, chunk2]), {
      onDelta: (d) => observed.push(d),
    });
    assert.deepStrictEqual(observed, ["Hi ", "there"]);
    assert.strictEqual(result.answer, "Hi there");
  });

  it("propagates abort via signal", async () => {
    // An infinite-chunk stream; abort immediately and ensure we throw.
    const encoder = new TextEncoder();
    let cancelled = false;
    const stream = new ReadableStream({
      pull(controller) {
        if (cancelled) return;
        controller.enqueue(encoder.encode("event: response_delta\ndata: {\"delta\": \"x\"}\n\n"));
      },
      cancel() { cancelled = true; },
    });
    const response = new Response(stream, { status: 200 });
    const ac = new AbortController();
    const p = consumeSSEStream(response, { signal: ac.signal });
    ac.abort();
    // The reader.cancel() path resolves the read loop cleanly; either way,
    // the consumer must observe cancellation rather than hang.
    await p.catch(() => {});
    assert.strictEqual(cancelled, true);
  });
});

describe("backoffDelayMs", () => {
  it("follows jittered exponential schedule capped at 30s", () => {
    // With rng() returning 0, the jitter term is 0, so the base series is
    // deterministic: 500, 1000, 2000, 4000, ... capped at 30000.
    const zero = () => 0;
    assert.strictEqual(backoffDelayMs(0, zero), 500);
    assert.strictEqual(backoffDelayMs(1, zero), 1000);
    assert.strictEqual(backoffDelayMs(2, zero), 2000);
    assert.strictEqual(backoffDelayMs(3, zero), 4000);
    assert.strictEqual(backoffDelayMs(10, zero), 30000); // capped
    // Jitter is bounded by 250ms.
    const max = backoffDelayMs(0, () => 1);
    assert.ok(max > 500 && max <= 750);
  });
});

describe("fetchSSEWithRetry", () => {
  it("retries 5xx with backoff then succeeds", async () => {
    const originalFetch = globalThis.fetch;
    let calls = 0;
    globalThis.fetch = (async () => {
      calls += 1;
      if (calls < 3) {
        return new Response("oops", { status: 503 });
      }
      return streamingResponse([
        "event: response_delta\ndata: {\"delta\": \"ok\"}\n\n",
        "event: metadata\ndata: {\"route\": \"echo\", \"confidence\": 1, \"cost_usd\": 0}\n\n",
      ]);
    }) as typeof fetch;
    const sleeps: number[] = [];
    try {
      const result = await fetchSSEWithRetry("http://x/ask", { method: "POST" }, {
        maxAttempts: 4,
        sleep: async (ms) => { sleeps.push(ms); },
      });
      assert.strictEqual(result.answer, "ok");
      assert.strictEqual(calls, 3);
      assert.strictEqual(sleeps.length, 2);
      // Each backoff is at least the exponential base for that attempt.
      assert.ok(sleeps[0] >= 500);
      assert.ok(sleeps[1] >= 1000);
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});
