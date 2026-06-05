import { describe, it } from "node:test";
import assert from "node:assert";
import { deriveSessionId } from "./session-id.js";

describe("deriveSessionId", () => {
  it("is stable for the same thread", () => {
    assert.strictEqual(deriveSessionId("slack:C1:t1"), deriveSessionId("slack:C1:t1"));
  });

  it("differs across threads, channels, and platforms (no merge, no bleed)", () => {
    assert.notStrictEqual(deriveSessionId("slack:C1:t1"), deriveSessionId("slack:C1:t2"));
    assert.notStrictEqual(deriveSessionId("slack:C1:t1"), deriveSessionId("slack:C2:t1"));
    assert.notStrictEqual(deriveSessionId("slack:C1:t1"), deriveSessionId("discord:C1:t1"));
  });

  it("is an opaque hash that doesn't leak the raw thread id", () => {
    const id = deriveSessionId("slack:C123:1700.0001");
    assert.match(id, /^botmem_[0-9a-f]{64}$/);
    assert.ok(!id.includes("slack"));
    assert.ok(!id.includes("C123"));
  });
});
