/**
 * End-to-end seam test: a realistic backend SSE byte stream → consumeSSEStream →
 * AskResult → renderResponse, asserting the whole reply pipeline composes
 * correctly (not just each unit in isolation).
 */
import { describe, it } from "node:test";
import assert from "node:assert";
import { consumeSSEStream } from "./sse-client.js";
import { renderResponse } from "./renderer.js";

function streamFrom(lines: string[]): Response {
  return new Response(lines.join("\n"), {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

describe("integration: SSE stream → rendered reply", () => {
  it("renders a full rich reply with every section in order", async () => {
    const syncTs = new Date(Date.now() - 2 * 3600_000).toISOString();
    const msgTs = new Date(Date.now() - 3 * 3600_000).toISOString();
    const body = [
      "event: response_delta",
      'data: {"delta": "We chose GridFS as the OSS default. "}',
      "",
      "event: response_delta",
      'data: {"delta": "MinIO is opt-in via config."}',
      "",
      "event: citations",
      'data: {"sources": [' +
        '{"kind": "wiki_page", "title": "Media storage", "permalink": "https://wiki/media"},' +
        `{"kind": "channel_message", "title": "no object store on OSS", "native": {"author": "Alan", "channel_name": "#tech", "message_ts": "${msgTs}"}},` +
        '{"kind": "decision_record", "title": "GridFS default decision"},' +
        '{"kind": "graph_relationship", "title": "Alan owns media-storage"}' +
        "]}",
      "",
      "event: follow_ups",
      'data: {"suggestions": ["How do I switch to MinIO?", "GridFS size limit?"]}',
      "",
      "event: related_context",
      'data: {"tensions": [{"title": "GridFS scalability disputed", "detail": "Alan: fine vs Thomas: risky"}]}',
      "",
      "event: metadata",
      `data: {"route": "qa_agent", "confidence": 0.91, "last_sync_ts": "${syncTs}"}`,
      "",
      "event: done",
      "data: {}",
      "",
    ];

    const result = await consumeSSEStream(streamFrom(body));

    // Parsed model is correct.
    assert.ok(result.answer.includes("GridFS as the OSS default"));
    assert.strictEqual(result.citations.length, 4);
    assert.deepStrictEqual(result.followUps, ["How do I switch to MinIO?", "GridFS size limit?"]);
    assert.strictEqual(result.tensions?.[0].title, "GridFS scalability disputed");
    assert.strictEqual(result.confidence, 0.91);
    assert.strictEqual(result.isEmpty, false);

    const out = renderResponse(result, "slack");
    // Answer first.
    assert.ok(out.startsWith("We chose GridFS"));
    // Sources block: canonical markdown heading, concise list items with a
    // clickable numbered marker `[N](url)`, and NO verbose fact text.
    assert.ok(out.includes("## 📎 Sources"));
    assert.ok(out.includes("- 📖 [1](https://wiki/media)"));
    // Full seam: wire `message_ts` → normalized `timestamp` → rendered age stamp.
    assert.ok(out.includes("- 💬 [2] Alan · #tech · 3h ago"));
    assert.ok(!out.includes("no object store on OSS"), "fact text should be dropped");
    // Related block has the graph/decision citations, with ORIGINAL indices.
    assert.ok(out.includes("## 🧠 Related"));
    assert.ok(out.includes("- ⚖️ [3]"));
    assert.ok(out.includes("- 🧠 [4]"));
    // Proactive tension heads-up (bold line, markdown bullet).
    assert.ok(out.includes("**⚠️ Heads up — possible tension**"));
    assert.ok(out.includes("- GridFS scalability disputed — Alan: fine vs Thomas: risky"));
    // Freshness (honest "last activity" label) + follow-ups + route.
    assert.ok(out.includes("🕐 _last activity 2h ago_"));
    assert.ok(out.includes("_You might also ask:_"));
    assert.ok(out.includes("- How do I switch to MinIO?"));
    // Internal route footer is suppressed (no "via qa_agent" leaked to users).
    assert.ok(!out.includes("via qa_agent"));
    // High confidence → NO warning.
    assert.ok(!out.includes("low confidence"));
  });

  it("renders the honest empty state when the backend flags empty retrieval", async () => {
    const body = [
      "event: response_delta",
      'data: {"delta": "This channel hasn\'t been synced yet."}',
      "",
      "event: citations",
      'data: {"items": []}',
      "",
      "event: metadata",
      'data: {"route": "qa_agent", "confidence": 0.15, "is_empty_retrieval": true}',
      "",
      "event: done",
      "data: {}",
      "",
    ];
    const result = await consumeSSEStream(streamFrom(body));
    assert.strictEqual(result.isEmpty, true);
    const out = renderResponse(result, "discord");
    assert.ok(/don't have anything indexed/i.test(out));
    assert.ok(!out.includes("## 📎 Sources"));
  });

  it("shows a low-confidence warning (right under the answer) when retrieval is thin", async () => {
    const body = [
      "event: response_delta",
      'data: {"delta": "Possibly, but I am not certain."}',
      "",
      "event: citations",
      'data: {"items": [{"type": "channel_message", "text": "a single weak hit"}]}',
      "",
      "event: metadata",
      'data: {"route": "qa_agent", "confidence": 0.2}',
      "",
      "event: done",
      "data: {}",
      "",
    ];
    const result = await consumeSSEStream(streamFrom(body));
    const out = renderResponse(result, "slack");
    assert.ok(out.includes("⚠️ _low confidence"));
    // Warning precedes the Sources block (truncation-safe placement).
    assert.ok(out.indexOf("low confidence") < out.indexOf("## 📎 Sources"));
  });
});
