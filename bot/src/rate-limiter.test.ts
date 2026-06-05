import { describe, it } from "node:test";
import assert from "node:assert";
import { RateLimiter } from "./rate-limiter.js";

describe("RateLimiter", () => {
  it("allows up to the limit then blocks with a retry hint", () => {
    const now = 0;
    const rl = new RateLimiter(3, 1000, () => now);
    assert.strictEqual(rl.check("k").allowed, true);
    assert.strictEqual(rl.check("k").allowed, true);
    assert.strictEqual(rl.check("k").allowed, true);
    const blocked = rl.check("k");
    assert.strictEqual(blocked.allowed, false);
    assert.ok(blocked.retryAfterMs > 0 && blocked.retryAfterMs <= 1000);
  });

  it("slides: allows again once the window passes", () => {
    let now = 0;
    const rl = new RateLimiter(1, 1000, () => now);
    assert.strictEqual(rl.check("k").allowed, true);
    assert.strictEqual(rl.check("k").allowed, false);
    now = 1001;
    assert.strictEqual(rl.check("k").allowed, true);
  });

  it("isolates keys (one user can't exhaust another)", () => {
    const now = 0;
    const rl = new RateLimiter(1, 1000, () => now);
    assert.strictEqual(rl.check("a").allowed, true);
    assert.strictEqual(rl.check("b").allowed, true);
    assert.strictEqual(rl.check("a").allowed, false);
  });

  it("stays functional after exceeding maxKeys (bounded memory)", () => {
    let now = 0;
    const rl = new RateLimiter(5, 1000, () => now, 3);
    for (let i = 0; i < 100; i++) {
      rl.check(`k${i}`);
      now += 1;
    }
    assert.strictEqual(rl.check("fresh").allowed, true);
  });
});
