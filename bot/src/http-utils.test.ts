/**
 * Tests for the response-side helpers in `http-utils.ts`.
 *
 * `messageForCode` is the canonical static-dictionary lookup that
 * replaces `safeErrorMessage(err)` at HTTP response sinks. It exists
 * to break the data-flow edge from caught `Error` values to the
 * response body that triggered CodeQL `js/stack-trace-exposure`
 * alert #60. The dictionary surface is small and stable; pinning it
 * here protects future contributors from accidentally widening the
 * keyspace or piping `err`-derived strings back through.
 */

import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { messageForCode } from "./http-utils.js";

describe("messageForCode", () => {
  it("maps every classifyPlatformError code to a static prose message", () => {
    assert.strictEqual(messageForCode("NOT_FOUND"), "resource not found");
    assert.strictEqual(messageForCode("FORBIDDEN"), "access denied");
    assert.strictEqual(messageForCode("RATE_LIMITED"), "rate limited");
    assert.strictEqual(messageForCode("NOT_SUPPORTED"), "operation not supported");
    assert.strictEqual(messageForCode("PLATFORM_ERROR"), "upstream platform error");
  });

  it("returns generic 'internal error' for unknown codes", () => {
    assert.strictEqual(messageForCode("WAT"), "internal error");
    assert.strictEqual(messageForCode(""), "internal error");
  });

  it("does not resolve via prototype-chain lookup", () => {
    // Guards against a future caller passing user input where a closed
    // enum was expected. `toString`, `hasOwnProperty`, etc. live on the
    // prototype and would resolve via plain bracket access; the helper
    // uses `Object.prototype.hasOwnProperty.call` so they fall through
    // to the generic message.
    assert.strictEqual(messageForCode("toString"), "internal error");
    assert.strictEqual(messageForCode("hasOwnProperty"), "internal error");
    assert.strictEqual(messageForCode("__proto__"), "internal error");
  });
});
