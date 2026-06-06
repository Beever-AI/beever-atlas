import { describe, it } from "node:test";
import assert from "node:assert";
import {
  consumeSSEStream,
  backoffDelayMs,
  fetchSSEWithRetry,
  normalizeCitations,
  detectEmptyRetrieval,
  resolveIsEmpty,
  normalizeTensions,
  dedupDoubledAnswer,
  dedupRepeats,
  scrubChannelId,
} from "./sse-client.js";

describe("dedupDoubledAnswer", () => {
  const A =
    "In the #basketball channel, SGA refers to Shai Gilgeous-Alexander, a top-3 player known for durability";
  it("collapses an exactly-doubled answer (same citation markers)", () => {
    const a = A + " [1]. He strengthens Team Canada [2].";
    assert.strictEqual(dedupDoubledAnswer(a + a), a);
  });
  it("collapses a citation-RENUMBERED doubled answer ([1][2] → [3][4])", () => {
    const c1 = A + " [1]. He strengthens Team Canada [2].";
    const c2 = A + " [3]. He strengthens Team Canada [4].";
    assert.strictEqual(dedupDoubledAnswer(c1 + c2), c1);
  });
  it("leaves a single (non-doubled) answer untouched", () => {
    assert.strictEqual(dedupDoubledAnswer(A), A);
  });
  it("leaves a short answer untouched", () => {
    assert.strictEqual(dedupDoubledAnswer("hi there"), "hi there");
  });
  it("leaves a legitimately repetitive-phrase answer untouched", () => {
    const legit = "The Celtics beat the Mavericks 4-1 [1]. The Celtics are champions [1]. More context here.";
    assert.strictEqual(dedupDoubledAnswer(legit), legit);
  });
  it("dedups the answer end-to-end through consumeSSEStream", async () => {
    const a = A + " [1]. He strengthens Team Canada [2].";
    // The backend streamed the full answer twice (the session-replay bug).
    const body =
      `event: response_delta\ndata: ${JSON.stringify({ delta: a })}\n\n` +
      `event: response_delta\ndata: ${JSON.stringify({ delta: a })}\n\n` +
      `event: metadata\ndata: {"route":"qa_agent","confidence":0.9}\n\n` +
      "event: done\ndata: {}\n\n";
    const result = await consumeSSEStream(mockResponse(body));
    assert.strictEqual(result.answer, a, "the rendered answer must not be doubled");
  });
  it("collapses a PARAPHRASE-double (re-worded second copy)", () => {
    const copy1 =
      "In this channel, Wembanyama has been highlighted for his unique skillset and impact on the NBA. " +
      "Sam Cheung considers him to have the rarest skillset available, ideal to build a franchise around [1]. " +
      "Ken Lau noted him among international MVPs like Doncic and Jokic, signifying global expansion [2].";
    const copy2 =
      "In this channel, Wembanyama is recognized for his unique skillset and his role in the NBA's global influence. " +
      "Sam Cheung suggested building a franchise around him due to his never-seen-before rarest skillset [1]. " +
      "Ken Lau included him among international players like Doncic and Jokic who won MVP awards [2].";
    const out = dedupRepeats(copy1 + copy2);
    assert.ok(out.length < (copy1 + copy2).length * 0.7, "paraphrase double should collapse");
    assert.strictEqual(
      (out.match(/In this channel, Wembanyama/g) || []).length,
      1,
      "only one copy of the opening should remain",
    );
  });
  it("does NOT collapse a legit two-part answer that shares vocabulary", () => {
    const legit =
      "Wembanyama plays for the San Antonio Spurs and was the first pick in the 2023 draft, " +
      "standing 7 foot 2 as the tallest player in the league. " +
      "His family is athletic: his father was a track athlete for Congo and his mother played for the French national team.";
    assert.strictEqual(dedupRepeats(legit), legit);
  });
  it("does NOT collapse a restate-then-EXPAND answer (same opening, new info)", () => {
    // A legit answer that restates its opening clause then adds detail — the
    // multi-sentence-repeat gate must keep it intact (only ONE segment matches).
    const restate =
      "The Postgres migration improves query latency and reduces storage costs across the analytics workload significantly this quarter. " +
      "The Postgres migration improves query latency by switching to columnar storage and reduces storage costs via better compression too.";
    assert.strictEqual(dedupRepeats(restate), restate, "expansion must not be dropped");
  });
});

describe("dedupRepeats — near-duplicate collapse", () => {
  const CHAN = "C0B5YCR1NL8";
  const HALF =
    "In the basketball channel, SGA refers to Shai Gilgeous-Alexander, a top-3 player known for durability and scoring.";

  it("collapses halves that differ only by a leaked (channelId)", () => {
    // First half carries the leaked id, second half doesn't — normalized equal.
    const a = `${HALF} (${CHAN})`;
    const b = HALF;
    const out = dedupRepeats(`${a} ${b}`, CHAN);
    // Collapsed to ONE copy of the substantive text. The `(id)` sits exactly at
    // the seam (a dropped span), so the kept prefix is the clean HALF — even
    // better, since scrubChannelId would strip the id anyway.
    assert.strictEqual(out.split("In the basketball channel").length - 1, 1, "collapsed to one copy");
    assert.ok(!out.includes(CHAN), "leaked id at the seam is dropped");
    assert.ok(out.includes("durability and scoring."));
  });

  it("collapses citation-renumbered halves ([1][2] → [3][4])", () => {
    const c1 = `${HALF} [1]. He strengthens Team Canada [2].`;
    const c2 = `${HALF} [3]. He strengthens Team Canada [4].`;
    assert.strictEqual(dedupRepeats(c1 + c2).trim(), c1.trim());
  });

  it("drops a repeated sentence (block-level), preserving the first", () => {
    const s = "The booth is H25 and staffing was confirmed by Jack last week.";
    const out = dedupRepeats(`${s} ${s} More unique context follows here.`);
    // The duplicate sentence appears exactly once.
    assert.strictEqual(out.split("The booth is H25").length - 1, 1);
    assert.ok(out.includes("More unique context follows here."));
  });

  it("leaves a genuinely non-repetitive answer unchanged", () => {
    const text =
      "Team Canada is strong because of SGA's two-way play. The roster also adds depth at guard, " +
      "and the coaching staff favors a switch-heavy defense that suits the personnel.";
    assert.strictEqual(dedupRepeats(text), text);
  });

  it("leaves a short legitimate repeat ('Yes. Yes.') unchanged", () => {
    // Each "Yes." block is < 40 chars, so block-level dedup must not touch it.
    const text =
      "Yes. Yes. The decision was ratified and the team agreed to proceed with the launch plan.";
    assert.strictEqual(dedupRepeats(text), text);
  });

  it("never guts a real answer down to near-empty (safety guard)", () => {
    // A contrived input where a split would otherwise leave <30 chars: the guard
    // returns the original instead.
    const text = "x".repeat(120);
    // An all-same-char string byte-doubles to 60 chars (>30), so it's safe to
    // collapse; assert it does NOT collapse below the guard floor.
    const out = dedupRepeats(text);
    assert.ok(out.length >= 30);
  });
});

describe("scrubChannelId", () => {
  const CHAN = "C0B5YCR1NL8";
  it("removes a parenthesized ( channelId ) form", () => {
    assert.strictEqual(
      scrubChannelId(`The booth is H25 (${CHAN}).`, CHAN),
      "The booth is H25.",
    );
  });
  it("removes a bare channel id occurrence", () => {
    assert.strictEqual(
      scrubChannelId(`See ${CHAN} for details.`, CHAN).replace(/\s+/g, " ").trim(),
      "See for details.",
    );
  });
  it("is a no-op when no channel id is provided", () => {
    assert.strictEqual(scrubChannelId(`keep ${CHAN}`, undefined), `keep ${CHAN}`);
  });
});

describe("finalizeResult — dedup + scrub end-to-end", () => {
  const CHAN = "C0B5YCR1NL8";
  it("scrubs the channel id from the rendered answer", async () => {
    const body =
      `event: response_delta\ndata: ${JSON.stringify({ delta: `The booth is H25 (${CHAN}).` })}\n\n` +
      `event: metadata\ndata: {"route":"qa_agent","confidence":0.9}\n\n` +
      "event: done\ndata: {}\n\n";
    const result = await consumeSSEStream(mockResponse(body), { channelId: CHAN });
    assert.ok(!result.answer.includes(CHAN), "leaked channel id must be scrubbed");
    assert.ok(result.answer.includes("The booth is H25."));
  });

  it("collapses an (id)-variant near-double and scrubs the id", async () => {
    const half =
      "In the basketball channel, SGA refers to Shai Gilgeous-Alexander, a durable top-3 scorer.";
    const a = `${half} (${CHAN})`;
    const body =
      `event: response_delta\ndata: ${JSON.stringify({ delta: `${a} ${half}` })}\n\n` +
      `event: metadata\ndata: {"route":"qa_agent","confidence":0.9}\n\n` +
      "event: done\ndata: {}\n\n";
    const result = await consumeSSEStream(mockResponse(body), { channelId: CHAN });
    // Only one copy survives AND the id is gone.
    assert.ok(!result.answer.includes(CHAN));
    assert.strictEqual(result.answer.split("In the basketball channel").length - 1, 1);
  });
});

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

describe("consumeSSEStream — enrichment", () => {
  it("propagates the empty-retrieval and freshness metadata", async () => {
    const body = [
      "event: response_delta",
      'data: {"delta": "This channel hasn\'t been synced yet."}',
      "",
      "event: citations",
      'data: {"items": []}',
      "",
      "event: metadata",
      'data: {"route": "qa_agent", "is_empty_retrieval": true, "last_sync_ts": "2026-06-01T00:00:00Z"}',
      "",
      "event: done",
      "data: {}",
      "",
    ].join("\n");
    const result = await consumeSSEStream(mockResponse(body));
    assert.strictEqual(result.isEmpty, true);
    assert.strictEqual(result.lastSyncTs, "2026-06-01T00:00:00Z");
  });

  it("normalizes registry-shape citations with provenance", async () => {
    const body = [
      "event: citations",
      'data: {"sources": [{"kind": "wiki_page", "title": "Booth", "permalink": "https://w/x", "native": {"author": "Jack", "channel_name": "#general"}}]}',
      "",
      "event: metadata",
      'data: {"route": "qa_agent"}',
      "",
      "event: done",
      "data: {}",
      "",
    ].join("\n");
    const result = await consumeSSEStream(mockResponse(body));
    assert.strictEqual(result.citations.length, 1);
    assert.strictEqual(result.citations[0].type, "wiki_page");
    assert.strictEqual(result.citations[0].author, "Jack");
    assert.strictEqual(result.citations[0].url, "https://w/x");
    assert.strictEqual(result.citations[0].source, "#general");
  });
});

describe("normalizeCitations", () => {
  it("maps legacy flat items", () => {
    const out = normalizeCitations({
      items: [
        { type: "fact", text: "sky is blue", author: "A", permalink: "u", channel: "#c", message_ts: "2026-05-21T19:14:37Z" },
      ],
    });
    assert.deepStrictEqual(out, [
      {
        type: "fact",
        text: "sky is blue",
        author: "A",
        url: "u",
        source: "#c",
        timestamp: "2026-05-21T19:14:37Z",
        title: undefined,
        platform: undefined,
      },
    ]);
  });
  it("skips items without text and prefers items over sources", () => {
    const out = normalizeCitations({
      items: [{ type: "fact" }, { type: "fact", text: "kept" }],
      sources: [{ kind: "wiki_page", title: "ignored" }],
    });
    assert.strictEqual(out.length, 1);
    assert.strictEqual(out[0].text, "kept");
  });
  it("returns [] for empty payloads", () => {
    assert.deepStrictEqual(normalizeCitations({}), []);
  });

  it("extracts title and platform from a registry source", () => {
    const out = normalizeCitations({
      sources: [
        {
          kind: "wiki_page",
          title: "Booth Plan",
          excerpt: "This channel is a lively hub… prima",
          permalink: "https://w/x",
          native: { platform: "discord", channel_name: "#general" },
        },
      ],
    });
    assert.strictEqual(out.length, 1);
    assert.strictEqual(out[0].title, "Booth Plan");
    assert.strictEqual(out[0].platform, "discord");
  });

  it("extracts title and platform from a legacy flat item", () => {
    const out = normalizeCitations({
      items: [{ type: "channel_message", text: "msg", title: "Topic", platform: "slack" }],
    });
    assert.strictEqual(out[0].title, "Topic");
    assert.strictEqual(out[0].platform, "slack");
  });

  it("collapses 3 identical sources to 1 (id-keyed)", () => {
    const dup = { kind: "wiki_page", title: "Same", permalink: "https://w/x", id: "src-7" };
    const out = normalizeCitations({ sources: [dup, dup, dup] });
    assert.strictEqual(out.length, 1);
    assert.strictEqual(out[0].title, "Same");
  });

  it("collapses identical sources by (type, url, text) when no id is present", () => {
    const dup = { type: "channel_message", text: "hello", permalink: "https://x/1" };
    const out = normalizeCitations({ items: [dup, dup, dup] });
    assert.strictEqual(out.length, 1);
  });

  it("preserves distinct sources (dedup must not over-collapse)", () => {
    const out = normalizeCitations({
      sources: [
        { kind: "wiki_page", title: "A", permalink: "https://w/a", id: "1" },
        { kind: "wiki_page", title: "B", permalink: "https://w/b", id: "2" },
        { kind: "wiki_page", title: "A", permalink: "https://w/a", id: "1" }, // dup of first
      ],
    });
    assert.strictEqual(out.length, 2);
    assert.deepStrictEqual(out.map((c) => c.title), ["A", "B"]);
  });
});

describe("consumeSSEStream — follow_ups", () => {
  it("captures up to 3 non-empty string suggestions", async () => {
    const body = [
      "event: response_delta",
      'data: {"delta": "answer"}',
      "",
      "event: follow_ups",
      'data: {"suggestions": ["What is X?", "  ", "How about Y?", 42, "Z?", "W?"]}',
      "",
      "event: metadata",
      'data: {"route": "qa_agent"}',
      "",
      "event: done",
      "data: {}",
      "",
    ].join("\n");
    const result = await consumeSSEStream(mockResponse(body));
    assert.deepStrictEqual(result.followUps, ["What is X?", "How about Y?", "Z?"]);
  });

  it("defaults followUps to [] when the event is absent", async () => {
    const body = [
      "event: response_delta",
      'data: {"delta": "answer"}',
      "",
      "event: metadata",
      'data: {"route": "qa_agent"}',
      "",
      "event: done",
      "data: {}",
      "",
    ].join("\n");
    const result = await consumeSSEStream(mockResponse(body));
    assert.deepStrictEqual(result.followUps, []);
  });
});

describe("consumeSSEStream — related_context", () => {
  it("captures tensions from the related_context event", async () => {
    const body = [
      "event: response_delta",
      'data: {"delta": "answer"}',
      "",
      "event: related_context",
      'data: {"tensions": [{"title": "Launch order", "detail": "marketing vs general"}], "extracted_entities": ["launch"]}',
      "",
      "event: metadata",
      'data: {"route": "qa_agent"}',
      "",
      "event: done",
      "data: {}",
      "",
    ].join("\n");
    const result = await consumeSSEStream(mockResponse(body));
    assert.strictEqual(result.tensions?.length, 1);
    assert.strictEqual(result.tensions?.[0].title, "Launch order");
    assert.strictEqual(result.tensions?.[0].detail, "marketing vs general");
  });

  it("defaults tensions to [] when the event is absent", async () => {
    const body = [
      "event: metadata",
      'data: {"route": "qa_agent"}',
      "",
      "event: done",
      "data: {}",
      "",
    ].join("\n");
    const result = await consumeSSEStream(mockResponse(body));
    assert.deepStrictEqual(result.tensions, []);
  });
});

describe("normalizeTensions", () => {
  it("maps alt field names and caps at 3", () => {
    const out = normalizeTensions({
      tensions: [
        { topic: "A", description: "da" },
        { title: "B" },
        { summary: "C" },
        { title: "D" },
      ],
    });
    assert.strictEqual(out.length, 3);
    assert.strictEqual(out[0].title, "A");
    assert.strictEqual(out[0].detail, "da");
    assert.strictEqual(out[1].title, "B");
  });
  it("skips entries with no title and tolerates junk", () => {
    assert.deepStrictEqual(normalizeTensions({ tensions: [{ detail: "x" }, 42, null] }), []);
    assert.deepStrictEqual(normalizeTensions({}), []);
  });
});

describe("resolveIsEmpty", () => {
  const noCites: never[] = [];
  it("trusts the backend empty flag for a short, uncited answer", () => {
    assert.strictEqual(resolveIsEmpty("nothing here", noCites, true), true);
  });
  it("does NOT hide a substantive answer even if backend flags empty", () => {
    assert.strictEqual(resolveIsEmpty("x".repeat(600), noCites, true), false);
  });
  it("treats the 600-char threshold as the substantive boundary", () => {
    // 599 chars is still collapsible when flagged empty; 600 is protected.
    assert.strictEqual(resolveIsEmpty("x".repeat(599), noCites, true), true);
    assert.strictEqual(resolveIsEmpty("x".repeat(600), noCites, true), false);
  });
  it("is never empty when citations exist", () => {
    assert.strictEqual(resolveIsEmpty("could not find any indexed", [{ type: "f", text: "t" }], true), false);
  });
  it("falls back to the text heuristic with no backend signal", () => {
    assert.strictEqual(resolveIsEmpty("I could not find any indexed memories", noCites, undefined), true);
    assert.strictEqual(resolveIsEmpty("The booth is H25.", noCites, undefined), false);
  });
  it("does NOT swallow a friendly greeting/identity reply flagged empty", () => {
    // No retrieval → no citations → backend flags empty, but this is a valid
    // conversational answer and must be shown verbatim (live-test bug fix).
    assert.strictEqual(
      resolveIsEmpty("Hi! I'm Beever Atlas, your team knowledge assistant. Ask me about your channels.", noCites, true),
      false,
    );
    assert.strictEqual(
      resolveIsEmpty("I don't have that indexed yet, but here's how to sync your workspace.", noCites, true),
      false,
    );
  });
  it("still collapses a definitive empty phrase even when it offers help", () => {
    // EMPTY_PATTERN keeps priority over the guidance guard, so a genuinely empty
    // answer can't slip through just because it also says "I can help".
    assert.strictEqual(
      resolveIsEmpty("I have no indexed memories yet, but I can help once it's synced.", noCites, true),
      true,
    );
    assert.strictEqual(resolveIsEmpty("This channel hasn't been synced yet.", noCites, true), true);
  });
});

describe("detectEmptyRetrieval", () => {
  it("flags empty when no citations and empty-pattern text", () => {
    assert.strictEqual(detectEmptyRetrieval("I could not find any indexed memories", []), true);
    assert.strictEqual(detectEmptyRetrieval("This channel hasn't been synced yet", []), true);
  });
  it("does not flag a real answer", () => {
    assert.strictEqual(detectEmptyRetrieval("The booth is H25.", []), false);
  });
  it("never flags empty when citations exist", () => {
    assert.strictEqual(
      detectEmptyRetrieval("no indexed memories", [{ type: "fact", text: "x" }]),
      false,
    );
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
