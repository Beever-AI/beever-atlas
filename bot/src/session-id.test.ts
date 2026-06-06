import { describe, it } from "node:test";
import assert from "node:assert";
import { deriveSessionId } from "./session-id.js";

// ── Threaded mode (isThreaded=true): keyed on the thread id ──────────────────

describe("deriveSessionId — threaded", () => {
  it("is stable for the same thread", () => {
    assert.strictEqual(
      deriveSessionId("t1", "U1", "C1", true),
      deriveSessionId("t1", "U1", "C1", true),
    );
  });

  it("is independent of user/channel in threaded mode (thread is the key)", () => {
    // Everyone sharing a thread shares its history → user/channel must not split it.
    assert.strictEqual(
      deriveSessionId("t1", "U1", "C1", true),
      deriveSessionId("t1", "U2", "C2", true),
    );
  });

  it("differs across threads (no merge of unrelated threads)", () => {
    assert.notStrictEqual(
      deriveSessionId("t1", "U1", "C1", true),
      deriveSessionId("t2", "U1", "C1", true),
    );
  });

  it("is an opaque hash that doesn't leak the raw thread id", () => {
    const id = deriveSessionId("slack-C123-1700.0001", "U1", "C123", true);
    assert.match(id, /^botmem_[0-9a-f]{64}$/);
    assert.ok(!id.includes("slack"));
    assert.ok(!id.includes("C123"));
  });
});

// ── Top-level mention mode (isThreaded=false): (user, channel, idle-bucket) ──

describe("deriveSessionId — loose top-level mention", () => {
  it("is stable within the same idle window for the same (user, channel)", () => {
    // Two calls in immediate succession land in the same 30-min bucket.
    assert.strictEqual(
      deriveSessionId("ignored", "U1", "C1", false),
      deriveSessionId("ignored", "U1", "C1", false),
    );
  });

  it("ignores the thread id in top-level mode", () => {
    assert.strictEqual(
      deriveSessionId("tA", "U1", "C1", false),
      deriveSessionId("tB", "U1", "C1", false),
    );
  });

  it("differs across users and channels (no bleed, no merge)", () => {
    assert.notStrictEqual(
      deriveSessionId("t", "U1", "C1", false),
      deriveSessionId("t", "U2", "C1", false),
    );
    assert.notStrictEqual(
      deriveSessionId("t", "U1", "C1", false),
      deriveSessionId("t", "U1", "C2", false),
    );
  });

  it("differs from the threaded key for the same inputs", () => {
    // The two modes hash different material, so a thread and a top-level mention
    // never collide into one session.
    assert.notStrictEqual(
      deriveSessionId("t1", "U1", "C1", true),
      deriveSessionId("t1", "U1", "C1", false),
    );
  });

  it("resets after the idle window (back-to-back continues, idle starts fresh)", (t) => {
    const IDLE_WINDOW_MS = 30 * 60 * 1000;
    // Pin Date.now to the START of a bucket so the +29m call stays in-window and
    // the +31m call crosses into the next bucket deterministically.
    const base = Math.floor(Date.now() / IDLE_WINDOW_MS) * IDLE_WINDOW_MS;
    const realNow = Date.now;
    let current = base;
    Date.now = () => current;
    t.after(() => { Date.now = realNow; });

    const first = deriveSessionId("t", "U1", "C1", false);
    // Same window (29 min later) → continuity.
    current = base + 29 * 60 * 1000;
    assert.strictEqual(deriveSessionId("t", "U1", "C1", false), first);
    // Past the window (31 min later) → a fresh session.
    current = base + 31 * 60 * 1000;
    assert.notStrictEqual(deriveSessionId("t", "U1", "C1", false), first);
  });

  it("treats an absent user id (\"unknown\") falsy-safely", () => {
    const id = deriveSessionId("t", "unknown", "C1", false);
    assert.match(id, /^botmem_[0-9a-f]{64}$/);
  });
});
