import { describe, it } from "node:test";
import assert from "node:assert";
import { ParticipantCache } from "./participant-cache.js";

describe("ParticipantCache", () => {
  it("returns undefined on a miss and the value after set", () => {
    const c = new ParticipantCache(1000, () => 0);
    assert.strictEqual(c.get("t1"), undefined);
    c.set("t1", 3);
    assert.strictEqual(c.get("t1"), 3);
  });

  it("expires entries after the TTL (injected clock)", () => {
    let now = 0;
    const c = new ParticipantCache(1000, () => now);
    c.set("t1", 2);
    now = 999;
    assert.strictEqual(c.get("t1"), 2);
    now = 1000;
    assert.strictEqual(c.get("t1"), undefined);
  });

  it("isolates entries per thread", () => {
    const c = new ParticipantCache(1000, () => 0);
    c.set("a", 1);
    c.set("b", 5);
    assert.strictEqual(c.get("a"), 1);
    assert.strictEqual(c.get("b"), 5);
  });

  it("is disabled when TTL <= 0", () => {
    const c = new ParticipantCache(0, () => 0);
    c.set("t1", 9);
    assert.strictEqual(c.get("t1"), undefined);
  });

  it("bounds memory: never exceeds maxEntries", () => {
    const c = new ParticipantCache(10_000, () => 0, 3);
    for (let i = 0; i < 50; i++) c.set(`t${i}`, i);
    let live = 0;
    for (let i = 0; i < 50; i++) if (c.get(`t${i}`) !== undefined) live += 1;
    assert.ok(live <= 3, `expected <=3 live entries, got ${live}`);
  });
});
