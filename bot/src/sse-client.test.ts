import { describe, it } from "node:test";
import assert from "node:assert";
import { consumeSSEStream } from "./sse-client.js";

function mockResponse(body: string): Response {
  return new Response(body, {
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
});
