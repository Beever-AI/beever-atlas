/**
 * Tests for the @mention image-vision path in index.ts.
 *
 * Locks the load-bearing contracts:
 *   1. extractImageAttachments returns `{ filename, extracted_text, mime_type }`
 *      dicts for image attachments, fetching bytes via the injected (bridge)
 *      proxyFile — NEVER a raw fetch.
 *   2. FAIL-OPEN: a proxyFile throw skips THAT image, the others continue, and
 *      no exception escapes.
 *   3. Caps: respects BOT_VISION_MAX_IMAGES and BOT_VISION_MAX_BYTES.
 *   4. askBackend includes `attachments` only when non-empty; the text-only
 *      body stays byte-identical (so existing SSE tests don't break).
 *   5. Gating: an image + short-text mention resolves to "respond".
 *
 * Env knobs are set BEFORE importing index.ts because the BOT_VISION_* consts
 * are read at module load. Each test file runs in its own process under
 * `tsx --test`, so this does not leak into other suites.
 */
import { describe, it, afterEach } from "node:test";
import assert from "node:assert";
import { Buffer } from "node:buffer";
// Type-only imports are erased at compile time, so they do NOT trigger module
// execution before the env is set below.
import type { ImageAttachment, PostableThread } from "./index.js";

// ESM value imports are hoisted, so the BOT_VISION_* consts in index.ts would be
// read with their defaults before any top-level `process.env` assignment ran.
// Set the env FIRST, then load index.ts via a dynamic import so the consts pick
// up these test values.
process.env.BOT_VISION_MAX_IMAGES = "2";
process.env.BOT_VISION_MAX_BYTES = "1000";
process.env.BRIDGE_API_KEY = "bridge-secret";

const {
  extractImageAttachments,
  decideMentionGate,
  resolvePostExtraction,
  isImageAttachment,
  answerInThread,
} = await import("./index.js");

type ProxyFile = (url: string) => Promise<{ contentType: string; buffer: Buffer }>;

const PNG_HEADER = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

function imageAtt(name: string, url = `https://files.slack.com/${name}`) {
  return { type: "image", mimeType: "image/png", name, url };
}

// A proxyFile that returns a small valid buffer for every url.
function okProxy(buf: Buffer = PNG_HEADER): ProxyFile {
  return async () => ({ contentType: "image/png", buffer: buf });
}

function extractResponse(text: string): Response {
  return new Response(
    JSON.stringify({ filename: "image", extracted_text: text, mime_type: "image/png" }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

describe("isImageAttachment", () => {
  it("matches by type and by image/* mime", () => {
    assert.ok(isImageAttachment({ type: "image" }));
    assert.ok(isImageAttachment({ mimeType: "image/jpeg" }));
    assert.ok(!isImageAttachment({ type: "file", mimeType: "application/pdf" }));
  });
});

describe("decideMentionGate", () => {
  it("makes an image + short-text mention answerable", () => {
    // Bare "what?" text alone would still be a question, so use empty text +
    // image: empty text → prompt, but the image override makes it respond.
    assert.strictEqual(decideMentionGate({ text: "", broadcast: false, hasImage: true }), "respond");
  });

  it("nudges (prompt) on an empty mention with NO image", () => {
    assert.strictEqual(decideMentionGate({ text: "", broadcast: false, hasImage: false }), "prompt");
  });

  it("leaves a normal text question answerable", () => {
    assert.strictEqual(
      decideMentionGate({ text: "what is our stack?", broadcast: false, hasImage: false }),
      "respond",
    );
  });
});

describe("resolvePostExtraction (gate recovery when images fail)", () => {
  it("proceeds when at least one image was described", () => {
    // Even if the text alone would have been a bare mention, a described image
    // is a real answerable subject.
    assert.strictEqual(resolvePostExtraction("prompt", true), "proceed");
    assert.strictEqual(resolvePostExtraction("skip", true), "proceed");
    assert.strictEqual(resolvePostExtraction("respond", true), "proceed");
  });

  it("falls back to the text-only intent when no image was described", () => {
    // The image override turned these into "respond"; with zero images
    // extracted we must honour what the text alone warranted.
    assert.strictEqual(resolvePostExtraction("prompt", false), "prompt"); // bare @mention → nudge
    assert.strictEqual(resolvePostExtraction("skip", false), "skip"); // announcement → stay quiet
    assert.strictEqual(resolvePostExtraction("respond", false), "proceed"); // real question → answer
  });
});

describe("extractImageAttachments", () => {
  const originalFetch = globalThis.fetch;
  afterEach(() => { globalThis.fetch = originalFetch; });

  it("returns extracted-text dicts for image attachments", async () => {
    globalThis.fetch = (async () => extractResponse("a chart of Q3 revenue")) as typeof fetch;
    const msg = { attachments: [imageAtt("a.png")], text: "what is this?" };
    const out = await extractImageAttachments(msg, "slack:C1:T1", () => okProxy());
    assert.strictEqual(out.length, 1);
    assert.strictEqual(out[0].extracted_text, "a chart of Q3 revenue");
    assert.strictEqual(out[0].mime_type, "image/png");
  });

  it("sends the bytes via proxyFile and posts to the internal endpoint with the BRIDGE key", async () => {
    let sentUrl = "";
    let sentAuth = "";
    let proxied = false;
    globalThis.fetch = (async (url: string, init: RequestInit) => {
      sentUrl = String(url);
      sentAuth = String((init.headers as Record<string, string>)["Authorization"] || "");
      return extractResponse("ok");
    }) as unknown as typeof fetch;
    const proxy: ProxyFile = async () => { proxied = true; return { contentType: "image/png", buffer: PNG_HEADER }; };
    await extractImageAttachments({ attachments: [imageAtt("a.png")] }, "slack:C1:T1", () => proxy);
    assert.ok(proxied, "bytes must be fetched via proxyFile, never a raw fetch");
    assert.ok(sentUrl.endsWith("/api/internal/media/extract-text"));
    assert.strictEqual(sentAuth, "Bearer bridge-secret");
  });

  it("FAIL-OPEN: a proxyFile throw skips that image, others continue, no throw escapes", async () => {
    globalThis.fetch = (async () => extractResponse("second image ok")) as typeof fetch;
    let call = 0;
    const proxy: ProxyFile = async () => {
      call += 1;
      if (call === 1) throw new Error("proxyFile boom");
      return { contentType: "image/png", buffer: PNG_HEADER };
    };
    const msg = { attachments: [imageAtt("a.png"), imageAtt("b.png")] };
    let out: ImageAttachment[] = [];
    await assert.doesNotReject(async () => {
      out = await extractImageAttachments(msg, "slack:C1:T1", () => proxy);
    });
    assert.strictEqual(out.length, 1, "the surviving image is still described");
    assert.strictEqual(out[0].extracted_text, "second image ok");
  });

  it("skips ALL images (no raw fetch) when no proxyFile resolves for the platform", async () => {
    let fetched = false;
    globalThis.fetch = (async () => { fetched = true; return extractResponse("x"); }) as typeof fetch;
    const out = await extractImageAttachments(
      { attachments: [imageAtt("a.png")] },
      "discord:G1:C1:T1",
      () => null, // no bridge for this platform
    );
    assert.strictEqual(out.length, 0);
    assert.ok(!fetched, "must NOT raw-fetch when there is no safe proxyFile");
  });

  it("respects BOT_VISION_MAX_IMAGES (=2 here)", async () => {
    let posts = 0;
    globalThis.fetch = (async () => { posts += 1; return extractResponse("ok"); }) as typeof fetch;
    const msg = { attachments: [imageAtt("a"), imageAtt("b"), imageAtt("c"), imageAtt("d")] };
    const out = await extractImageAttachments(msg, "slack:C1:T1", () => okProxy());
    assert.strictEqual(out.length, 2);
    assert.strictEqual(posts, 2, "only the first MAX_IMAGES are processed");
  });

  it("respects BOT_VISION_MAX_BYTES (=1000 here) and skips an oversized image", async () => {
    let posted = false;
    globalThis.fetch = (async () => { posted = true; return extractResponse("ok"); }) as typeof fetch;
    const big = Buffer.alloc(2000, 1); // > 1000-byte cap
    const out = await extractImageAttachments(
      { attachments: [imageAtt("big.png")] },
      "slack:C1:T1",
      () => okProxy(big),
    );
    assert.strictEqual(out.length, 0);
    assert.ok(!posted, "oversized image is dropped before the extract POST");
  });

  it("returns [] for a message with no image attachments", async () => {
    const out = await extractImageAttachments(
      { attachments: [{ type: "file", mimeType: "application/pdf", url: "u" }] },
      "slack:C1:T1",
      () => okProxy(),
    );
    assert.strictEqual(out.length, 0);
  });
});

// ── askBackend body shape (via answerInThread) ───────────────────────────────

const HAPPY_SSE =
  'event: response_delta\ndata: {"delta": "It is a bar chart."}\n\n' +
  'event: metadata\ndata: {"route": "qa_agent", "confidence": 0.9}\n\n' +
  "event: done\ndata: {}\n\n";

function sseResponse(body: string): Response {
  return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

describe("askBackend attachments plumbing", () => {
  const originalFetch = globalThis.fetch;
  afterEach(() => { globalThis.fetch = originalFetch; });

  it("includes `attachments` in the body ONLY when non-empty", async () => {
    let sentBody: Record<string, unknown> = {};
    globalThis.fetch = (async (_url: string, init: RequestInit) => {
      sentBody = JSON.parse(String(init.body));
      return sseResponse(HAPPY_SSE);
    }) as unknown as typeof fetch;
    const thread: PostableThread = { id: "slack:C1:T1", post: async () => {} };
    const atts: ImageAttachment[] = [
      { filename: "a.png", extracted_text: "a chart", mime_type: "image/png" },
    ];
    await answerInThread(thread, "what is this?", "mention", { userId: "U1" }, true, atts);
    assert.deepStrictEqual(sentBody.attachments, atts);
  });

  it("keeps the text-only body byte-identical to today (no attachments key)", async () => {
    let rawBody = "";
    globalThis.fetch = (async (_url: string, init: RequestInit) => {
      rawBody = String(init.body);
      return sseResponse(HAPPY_SSE);
    }) as unknown as typeof fetch;
    const thread: PostableThread = { id: "slack:C1:T1", post: async () => {} };
    // No attachments arg at all → behaves exactly like the legacy call.
    await answerInThread(thread, "why?", "mention", { userId: "U1" }, true);
    const parsed = JSON.parse(rawBody);
    assert.ok(!("attachments" in parsed), "text-only body must not carry an attachments key");
    assert.deepStrictEqual(Object.keys(parsed).sort(), ["question", "session_id", "user_id"].sort());
  });

  it("omits `attachments` when an empty array is passed (all images failed)", async () => {
    let parsed: Record<string, unknown> = {};
    globalThis.fetch = (async (_url: string, init: RequestInit) => {
      parsed = JSON.parse(String(init.body));
      return sseResponse(HAPPY_SSE);
    }) as unknown as typeof fetch;
    const thread: PostableThread = { id: "slack:C1:T1", post: async () => {} };
    await answerInThread(thread, "q", "mention", { userId: "U1" }, true, []);
    assert.ok(!("attachments" in parsed));
  });
});
