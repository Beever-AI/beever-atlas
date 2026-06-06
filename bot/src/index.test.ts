/**
 * Handler-level tests for the reply path in index.ts.
 *
 * These lock two load-bearing contracts the renderer/sse-client unit tests
 * can't see:
 *   1. answerInThread posts an `{ markdown }` ENVELOPE (not a raw string) — the
 *      whole point of the presentation fix; a regression to a raw string would
 *      reintroduce literal `##`/`**` on Slack/Teams/Telegram.
 *   2. postNotice prefers an ephemeral message (with `{ fallbackToDM: true }`)
 *      and degrades to a normal post when ephemeral is unavailable or fails,
 *      threading the message author through.
 *
 * Importing index.ts is side-effect free: `main()` is guarded behind an
 * entrypoint check, so this import does not start the HTTP server.
 */
import { describe, it, afterEach } from "node:test";
import assert from "node:assert";
import { answerInThread, postNotice, type PostableThread } from "./index.js";

// ── postNotice ────────────────────────────────────────────────────────────────

describe("postNotice", () => {
  it("posts ephemeral (with DM fallback) when author + postEphemeral are present", async () => {
    const calls: Array<{ kind: string; args: unknown[] }> = [];
    const thread: PostableThread = {
      id: "slack:C1:T1",
      post: async (m) => { calls.push({ kind: "post", args: [m] }); },
      postEphemeral: async (u, m, o) => { calls.push({ kind: "ephemeral", args: [u, m, o] }); return null; },
    };
    await postNotice(thread, { userId: "U1" }, "heads up");
    assert.strictEqual(calls.length, 1);
    assert.strictEqual(calls[0].kind, "ephemeral");
    assert.deepStrictEqual(calls[0].args[0], { userId: "U1" });
    assert.strictEqual(calls[0].args[1], "heads up");
    assert.deepStrictEqual(calls[0].args[2], { fallbackToDM: true });
  });

  it("falls back to a normal post when postEphemeral rejects, without throwing", async () => {
    const calls: string[] = [];
    const thread: PostableThread = {
      id: "slack:C1:T1",
      post: async () => { calls.push("post"); },
      postEphemeral: async () => { throw new Error("ephemeral not supported here"); },
    };
    await assert.doesNotReject(postNotice(thread, { userId: "U1" }, "x"));
    assert.deepStrictEqual(calls, ["post"]);
  });

  it("posts directly when the platform has no postEphemeral", async () => {
    const calls: string[] = [];
    const thread: PostableThread = {
      id: "discord:C1:T1",
      post: async () => { calls.push("post"); },
    };
    await postNotice(thread, { userId: "U1" }, "x");
    assert.deepStrictEqual(calls, ["post"]);
  });

  it("posts directly (skips ephemeral) when there is no author", async () => {
    const calls: string[] = [];
    const thread: PostableThread = {
      id: "slack:C1:T1",
      post: async () => { calls.push("post"); },
      postEphemeral: async () => { calls.push("ephemeral"); return null; },
    };
    await postNotice(thread, undefined, "x");
    assert.deepStrictEqual(calls, ["post"]);
  });
});

// ── answerInThread ──────────────────────────────────────────────────────────────

function sseResponse(body: string): Response {
  return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

const HAPPY_SSE =
  'event: response_delta\ndata: {"delta": "Team Canada is strong because of SGA."}\n\n' +
  'event: metadata\ndata: {"route": "qa_agent", "confidence": 0.9}\n\n' +
  "event: done\ndata: {}\n\n";

describe("answerInThread", () => {
  const originalFetch = globalThis.fetch;
  afterEach(() => { globalThis.fetch = originalFetch; });

  it("posts the reply as a { markdown } envelope, not a raw string", async () => {
    globalThis.fetch = (async () => sseResponse(HAPPY_SSE)) as typeof fetch;
    let posted: unknown;
    const thread: PostableThread = {
      id: "slack:C0B5YCR1NL8:1700000000.0001",
      post: async (m) => { posted = m; },
      startTyping: async () => {},
    };
    await answerInThread(thread, "why is Team Canada good?", "mention", { userId: "U1" });
    // The load-bearing contract: an object { markdown: <string> }, NEVER a string.
    assert.strictEqual(typeof posted, "object", "reply must be a { markdown } object, not a raw string");
    assert.ok(posted && typeof (posted as { markdown?: unknown }).markdown === "string");
    assert.ok((posted as { markdown: string }).markdown.includes("Team Canada is strong"));
  });

  it("invokes startTyping when available and still completes if it rejects", async () => {
    globalThis.fetch = (async () => sseResponse(HAPPY_SSE)) as typeof fetch;
    let typed = false;
    let posted: unknown;
    const thread: PostableThread = {
      id: "slack:C1:T1",
      post: async (m) => { posted = m; },
      startTyping: async () => { typed = true; throw new Error("typing unsupported"); },
    };
    await assert.doesNotReject(answerInThread(thread, "hi", "mention", { userId: "U1" }));
    assert.ok(typed, "startTyping should be invoked");
    assert.ok(posted, "reply should still be posted despite a typing failure");
  });

  it("plumbs user_id and a session_id into the backend POST body", async () => {
    let sentBody: Record<string, unknown> = {};
    globalThis.fetch = (async (_url: string, init: RequestInit) => {
      sentBody = JSON.parse(String(init.body));
      return sseResponse(HAPPY_SSE);
    }) as unknown as typeof fetch;
    const thread: PostableThread = {
      id: "slack:C0B5YCR1NL8:1700000000.0001",
      post: async () => {},
    };
    await answerInThread(thread, "why?", "mention", { userId: "U1" });
    assert.strictEqual(sentBody.user_id, "U1");
    assert.strictEqual(sentBody.question, "why?");
    assert.strictEqual(typeof sentBody.session_id, "string");
  });

  it("defaults user_id to \"unknown\" when the author has no id", async () => {
    let sentBody: Record<string, unknown> = {};
    globalThis.fetch = (async (_url: string, init: RequestInit) => {
      sentBody = JSON.parse(String(init.body));
      return sseResponse(HAPPY_SSE);
    }) as unknown as typeof fetch;
    const thread: PostableThread = { id: "slack:C1:T1", post: async () => {} };
    await answerInThread(thread, "q", "mention");
    assert.strictEqual(sentBody.user_id, "unknown");
  });

  it("derives different session ids for threaded vs loose top-level mentions", async () => {
    const sessions: string[] = [];
    globalThis.fetch = (async (_url: string, init: RequestInit) => {
      sessions.push(JSON.parse(String(init.body)).session_id);
      return sseResponse(HAPPY_SSE);
    }) as unknown as typeof fetch;
    const thread: PostableThread = { id: "slack:C1:T1", post: async () => {} };
    await answerInThread(thread, "q", "mention", { userId: "U1" }, true); // threaded
    await answerInThread(thread, "q", "mention", { userId: "U1" }, false); // top-level
    assert.notStrictEqual(sessions[0], sessions[1]);
  });

  it("P2: a root @mention and its thread replies share ONE session id", async () => {
    // Both handlers key on the thread when a thread root exists (isThreaded=true),
    // and thread.id is the stable thread root → root + replies resolve to one id.
    const sessions: string[] = [];
    globalThis.fetch = (async (_url: string, init: RequestInit) => {
      sessions.push(JSON.parse(String(init.body)).session_id);
      return sseResponse(HAPPY_SSE);
    }) as unknown as typeof fetch;
    const thread: PostableThread = { id: "slack:C0B5YCR1NL8:1700000000.0001", post: async () => {} };
    await answerInThread(thread, "root mention", "mention", { userId: "U1" }, true);
    await answerInThread(thread, "first reply", "follow-up", { userId: "U1" }, true);
    await answerInThread(thread, "later reply", "follow-up", { userId: "U2" }, true);
    assert.strictEqual(sessions[0], sessions[1]);
    assert.strictEqual(sessions[0], sessions[2]);
  });

  it("delivers an ephemeral error notice when the backend call fails", async () => {
    // A 4xx is terminal (no retry) → answerInThread swallows it into a notice.
    globalThis.fetch = (async () => new Response("bad request", { status: 400 })) as typeof fetch;
    const calls: string[] = [];
    const thread: PostableThread = {
      id: "slack:C1:T1",
      post: async () => { calls.push("post"); },
      postEphemeral: async () => { calls.push("ephemeral"); return null; },
    };
    await assert.doesNotReject(answerInThread(thread, "q", "mention", { userId: "U1" }));
    // Error went out ephemeral (channel stays clean), not as a permanent post.
    assert.deepStrictEqual(calls, ["ephemeral"]);
  });
});
