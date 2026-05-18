import { describe, it } from "node:test";
import assert from "node:assert";
import { ChatManager } from "./chat-manager.js";

// ── Helpers ───────────────────────────────────────────────────────────────────

function makeChatManager(): ChatManager {
  return new ChatManager("redis://localhost:6379", () => {});
}

/** Return a ChatManager with rebuild() stubbed out (no network calls). */
function makeStubbedManager(): ChatManager {
  const cm = makeChatManager();
  (cm as any).rebuild = async () => {};
  return cm;
}

// ── Initial state ─────────────────────────────────────────────────────────────

describe("ChatManager — initial state", () => {
  it("getCurrentBot() returns null before any registration", () => {
    const cm = makeChatManager();
    assert.strictEqual(cm.getCurrentBot(), null);
  });

  it("isTransitioning() returns false before any registration", () => {
    const cm = makeChatManager();
    assert.strictEqual(cm.isTransitioning(), false);
  });

  it("listAdapters() returns empty array before any registration", () => {
    const cm = makeChatManager();
    assert.deepStrictEqual(cm.listAdapters(), []);
  });

  it("getAdapter() returns null when no bot exists", () => {
    const cm = makeChatManager();
    assert.strictEqual(cm.getAdapter("slack"), null);
  });
});

// ── Composite key registration ──────────────────────────────────────────────

describe("ChatManager — composite key registration", () => {
  it("register() with connectionId uses composite key", async () => {
    const cm = makeStubbedManager();

    await cm.register("slack", { botToken: "xoxb-1", signingSecret: "s1" }, "conn-1");

    const adapters = cm.listAdapters();
    assert.strictEqual(adapters.length, 1);
    assert.strictEqual(adapters[0].platform, "slack");
    assert.strictEqual(adapters[0].connectionId, "conn-1");
  });

  it("register() without connectionId falls back to platform as ID", async () => {
    const cm = makeStubbedManager();

    await cm.register("slack", { botToken: "xoxb-1", signingSecret: "s1" });

    const adapters = cm.listAdapters();
    assert.strictEqual(adapters.length, 1);
    assert.strictEqual(adapters[0].connectionId, "slack");
  });

  it("supports multiple connections for the same platform", async () => {
    const cm = makeStubbedManager();

    await cm.register("slack", { botToken: "xoxb-1", signingSecret: "s1" }, "conn-1");
    await cm.register("slack", { botToken: "xoxb-2", signingSecret: "s2" }, "conn-2");

    const adapters = cm.listAdapters();
    assert.strictEqual(adapters.length, 2);
    const ids = adapters.map((a) => a.connectionId).sort();
    assert.deepStrictEqual(ids, ["conn-1", "conn-2"]);
    assert.ok(adapters.every((a) => a.platform === "slack"));
  });

  it("supports mixed platform connections", async () => {
    const cm = makeStubbedManager();

    await cm.register("slack", { botToken: "xoxb-1", signingSecret: "s1" }, "conn-1");
    await cm.register("discord", { token: "discord-tok" }, "conn-2");

    const adapters = cm.listAdapters();
    assert.strictEqual(adapters.length, 2);
    const platforms = adapters.map((a) => a.platform).sort();
    assert.deepStrictEqual(platforms, ["discord", "slack"]);
  });
});

// ── Unregister ──────────────────────────────────────────────────────────────

describe("ChatManager — unregister", () => {
  it("unregister by platform and connectionId removes only that entry", async () => {
    const cm = makeStubbedManager();

    await cm.register("slack", { botToken: "xoxb-1", signingSecret: "s1" }, "conn-1");
    await cm.register("slack", { botToken: "xoxb-2", signingSecret: "s2" }, "conn-2");
    await cm.unregister("slack", "conn-1");

    const adapters = cm.listAdapters();
    assert.strictEqual(adapters.length, 1);
    assert.strictEqual(adapters[0].connectionId, "conn-2");
  });

  it("unregisterByConnectionId removes the correct entry", async () => {
    const cm = makeStubbedManager();

    await cm.register("slack", { botToken: "xoxb-1", signingSecret: "s1" }, "conn-1");
    await cm.register("slack", { botToken: "xoxb-2", signingSecret: "s2" }, "conn-2");

    const found = await cm.unregisterByConnectionId("conn-1");

    assert.strictEqual(found, true);
    const adapters = cm.listAdapters();
    assert.strictEqual(adapters.length, 1);
    assert.strictEqual(adapters[0].connectionId, "conn-2");
  });

  it("unregisterByConnectionId returns false for unknown ID", async () => {
    const cm = makeStubbedManager();

    await cm.register("slack", { botToken: "xoxb-1", signingSecret: "s1" }, "conn-1");

    const found = await cm.unregisterByConnectionId("unknown");

    assert.strictEqual(found, false);
    assert.strictEqual(cm.listAdapters().length, 1);
  });

  it("unregister without connectionId uses platform as fallback", async () => {
    const cm = makeStubbedManager();

    await cm.register("slack", { botToken: "xoxb-1", signingSecret: "s1" }); // key: slack:slack
    await cm.unregister("slack"); // should remove slack:slack

    assert.strictEqual(cm.listAdapters().length, 0);
  });
});

// ── Lookup methods ──────────────────────────────────────────────────────────

describe("ChatManager — lookup methods", () => {
  it("getAdapterByConnectionId returns null when no bot exists", async () => {
    const cm = makeStubbedManager();

    await cm.register("slack", { botToken: "xoxb-1", signingSecret: "s1" }, "conn-1");

    // currentBot is null because rebuild was stubbed
    assert.strictEqual(cm.getAdapterByConnectionId("conn-1"), null);
  });

  it("getAdapterByConnectionId finds adapter when bot has matching key", async () => {
    const cm = makeStubbedManager();

    await cm.register("slack", { botToken: "xoxb-1", signingSecret: "s1" }, "conn-1");

    // Simulate a bot with the adapter
    (cm as any).currentBot = { adapters: new Map([["slack:conn-1", { fake: true }]]) };

    const result = cm.getAdapterByConnectionId("conn-1");
    assert.ok(result);
    assert.strictEqual(result!.platform, "slack");
    assert.strictEqual(result!.connectionId, "conn-1");
  });

  it("getAdaptersByPlatform returns all adapters for a platform", async () => {
    const cm = makeStubbedManager();

    await cm.register("slack", { botToken: "xoxb-1", signingSecret: "s1" }, "conn-1");
    await cm.register("slack", { botToken: "xoxb-2", signingSecret: "s2" }, "conn-2");

    (cm as any).currentBot = {
      adapters: new Map([
        ["slack:conn-1", { fake: 1 }],
        ["slack:conn-2", { fake: 2 }],
      ]),
    };

    const results = cm.getAdaptersByPlatform("slack");
    assert.strictEqual(results.length, 2);
    const ids = results.map((r) => r.connectionId).sort();
    assert.deepStrictEqual(ids, ["conn-1", "conn-2"]);
  });

  it("getAdaptersByPlatform returns empty for unregistered platform", async () => {
    const cm = makeStubbedManager();

    await cm.register("slack", { botToken: "xoxb-1", signingSecret: "s1" }, "conn-1");

    (cm as any).currentBot = { adapters: new Map([["slack:conn-1", { fake: true }]]) };

    const results = cm.getAdaptersByPlatform("discord");
    assert.strictEqual(results.length, 0);
  });

  it("getCompositeKeyForConnection returns the composite key", async () => {
    const cm = makeStubbedManager();

    await cm.register("slack", { botToken: "xoxb-1", signingSecret: "s1" }, "conn-1");

    assert.strictEqual(cm.getCompositeKeyForConnection("conn-1"), "slack:conn-1");
  });

  it("getCompositeKeyForConnection returns null for unknown ID", async () => {
    const cm = makeStubbedManager();

    assert.strictEqual(cm.getCompositeKeyForConnection("unknown"), null);
  });

  it("getAdapter with composite key finds adapter in bot", async () => {
    const cm = makeStubbedManager();

    await cm.register("slack", { botToken: "xoxb-1", signingSecret: "s1" }, "conn-1");

    (cm as any).currentBot = { adapters: new Map([["slack:conn-1", { fake: true }]]) };

    const adapter = cm.getAdapter("slack:conn-1");
    assert.ok(adapter);
  });

  it("getAdapter with platform name falls back to first match", async () => {
    const cm = makeStubbedManager();

    await cm.register("slack", { botToken: "xoxb-1", signingSecret: "s1" }, "conn-1");

    (cm as any).currentBot = { adapters: new Map([["slack:conn-1", { fake: true }]]) };

    const adapter = cm.getAdapter("slack");
    assert.ok(adapter);
  });
});

// ── listAdapters metadata ──────────────────────────────────────────────────

describe("ChatManager — listAdapters() metadata", () => {
  it("returns platform, connectionId, and status for each adapter", async () => {
    const cm = makeStubbedManager();

    await cm.register("slack", { botToken: "tok", signingSecret: "sec" }, "conn-1");

    (cm as any).currentBot = { adapters: new Map([["slack:conn-1", { fake: true }]]) };

    const [entry] = cm.listAdapters();
    assert.strictEqual(entry.platform, "slack");
    assert.strictEqual(entry.connectionId, "conn-1");
    assert.strictEqual(entry.status, "connected");
  });

  it("status is 'error' when currentBot is null", async () => {
    const cm = makeStubbedManager();

    await cm.register("slack", { botToken: "tok", signingSecret: "sec" }, "conn-1");

    const [entry] = cm.listAdapters();
    assert.strictEqual(entry.status, "error");
  });
});

// ── isTransitioning flag ────────────────────────────────────────────────────

describe("ChatManager — isTransitioning flag", () => {
  it("isTransitioning() is true during rebuild and false after", async () => {
    const cm = makeChatManager();
    const flagDuringRebuild: boolean[] = [];

    (cm as any).rebuild = async () => {
      (cm as any).transitioning = true;
      flagDuringRebuild.push(cm.isTransitioning());
      (cm as any).transitioning = false;
    };

    await cm.register("slack", { botToken: "tok", signingSecret: "sec" }, "conn-1");

    assert.strictEqual(flagDuringRebuild[0], true);
    assert.strictEqual(cm.isTransitioning(), false);
  });
});

// ── Scheduled adapter recycle (RES-286) ─────────────────────────────────────

describe("ChatManager — scheduleAdapterRecycle()", () => {
  it("fires rebuild() on the configured interval", async () => {
    const cm = makeChatManager();
    let rebuildCount = 0;
    (cm as any).rebuild = async () => {
      rebuildCount++;
    };

    // Seed an adapter so the recycle path doesn't early-return.
    (cm as any).adapters.set("slack:conn-1", {
      platform: "slack",
      connectionId: "conn-1",
      config: { botToken: "x", signingSecret: "y" },
    });

    cm.scheduleAdapterRecycle(20);
    await new Promise((r) => setTimeout(r, 70));
    cm.stopAdapterRecycle();

    assert.ok(rebuildCount >= 2, `expected at least 2 rebuilds, got ${rebuildCount}`);
  });

  it("skips recycle when no adapters are registered", async () => {
    const cm = makeChatManager();
    let rebuildCount = 0;
    (cm as any).rebuild = async () => {
      rebuildCount++;
    };

    cm.scheduleAdapterRecycle(15);
    await new Promise((r) => setTimeout(r, 60));
    cm.stopAdapterRecycle();

    assert.strictEqual(rebuildCount, 0);
  });

  it("skips recycle while transitioning to avoid concurrent rebuild", async () => {
    const cm = makeChatManager();
    let rebuildCount = 0;
    (cm as any).rebuild = async () => {
      rebuildCount++;
    };
    (cm as any).adapters.set("slack:conn-1", {
      platform: "slack",
      connectionId: "conn-1",
      config: {},
    });
    (cm as any).transitioning = true;

    cm.scheduleAdapterRecycle(15);
    await new Promise((r) => setTimeout(r, 60));
    cm.stopAdapterRecycle();

    assert.strictEqual(rebuildCount, 0);
  });

  it("intervalMs <= 0 disables the timer (no recycle)", async () => {
    const cm = makeChatManager();
    let rebuildCount = 0;
    (cm as any).rebuild = async () => {
      rebuildCount++;
    };
    (cm as any).adapters.set("slack:conn-1", {
      platform: "slack",
      connectionId: "conn-1",
      config: {},
    });

    cm.scheduleAdapterRecycle(0);
    await new Promise((r) => setTimeout(r, 40));
    cm.stopAdapterRecycle();

    assert.strictEqual(rebuildCount, 0);
  });

  it("calling scheduleAdapterRecycle twice replaces the timer (no double-fire)", async () => {
    const cm = makeChatManager();
    let rebuildCount = 0;
    (cm as any).rebuild = async () => {
      rebuildCount++;
    };
    (cm as any).adapters.set("slack:conn-1", {
      platform: "slack",
      connectionId: "conn-1",
      config: {},
    });

    cm.scheduleAdapterRecycle(15);
    cm.scheduleAdapterRecycle(15);
    await new Promise((r) => setTimeout(r, 50));
    cm.stopAdapterRecycle();

    // With a single active timer at 15ms, expect ~3 fires in 50ms — not 6.
    assert.ok(rebuildCount <= 4, `expected ≤4 rebuilds (single timer), got ${rebuildCount}`);
  });

  it("stopAdapterRecycle() halts further rebuilds", async () => {
    const cm = makeChatManager();
    let rebuildCount = 0;
    (cm as any).rebuild = async () => {
      rebuildCount++;
    };
    (cm as any).adapters.set("slack:conn-1", {
      platform: "slack",
      connectionId: "conn-1",
      config: {},
    });

    cm.scheduleAdapterRecycle(15);
    await new Promise((r) => setTimeout(r, 50));
    cm.stopAdapterRecycle();
    const countAfterStop = rebuildCount;
    await new Promise((r) => setTimeout(r, 60));

    assert.strictEqual(rebuildCount, countAfterStop);
  });

  it("swallows rebuild() errors and keeps ticking (until failure limit)", async () => {
    const cm = makeChatManager();
    let attempts = 0;
    (cm as any).rebuild = async () => {
      attempts++;
      throw new Error("simulated rebuild failure");
    };
    (cm as any).adapters.set("slack:conn-1", {
      platform: "slack",
      connectionId: "conn-1",
      config: {},
    });

    cm.scheduleAdapterRecycle(15);
    await new Promise((r) => setTimeout(r, 60));
    cm.stopAdapterRecycle();

    // Should have attempted at least 2 ticks before the circuit breaker stops
    // it (RECYCLE_FAILURE_LIMIT is 3). The .catch() must not crash the timer.
    assert.ok(attempts >= 2, `expected at least 2 attempts, got ${attempts}`);
  });

  it("circuit breaker halts the timer after RECYCLE_FAILURE_LIMIT consecutive failures", async () => {
    const cm = makeChatManager();
    let attempts = 0;
    (cm as any).rebuild = async () => {
      attempts++;
      throw new Error("simulated rebuild failure");
    };
    (cm as any).adapters.set("slack:conn-1", {
      platform: "slack",
      connectionId: "conn-1",
      config: {},
    });

    cm.scheduleAdapterRecycle(10);
    // Wait long enough that the breaker would have tripped (3 failures × 10 ms
    // + some scheduling slack), then a stretch where further ticks could fire.
    await new Promise((r) => setTimeout(r, 200));
    const attemptsAfterTrip = attempts;
    await new Promise((r) => setTimeout(r, 100));

    // After the breaker trips no further attempts should occur.
    assert.strictEqual(attempts, attemptsAfterTrip);
    // And the trip should have happened at exactly RECYCLE_FAILURE_LIMIT (3)
    // — not earlier and not (much) later.
    assert.ok(attempts >= 3, `expected ≥3 attempts before trip, got ${attempts}`);
    assert.ok(attempts <= 4, `expected ≤4 attempts (timer race), got ${attempts}`);
  });

  it("resets consecutive-failure counter after a successful rebuild", async () => {
    const cm = makeChatManager();
    let attempts = 0;
    (cm as any).rebuild = async () => {
      attempts++;
      // Fail every other tick — the breaker should NEVER trip because a
      // success resets the counter.
      if (attempts % 2 === 1) throw new Error("flaky");
    };
    (cm as any).adapters.set("slack:conn-1", {
      platform: "slack",
      connectionId: "conn-1",
      config: {},
    });

    cm.scheduleAdapterRecycle(10);
    await new Promise((r) => setTimeout(r, 200));
    cm.stopAdapterRecycle();

    // Without reset, attempts would max out at 3-4 before the breaker fires.
    // With reset, we expect many more attempts in 200 ms × 10 ms interval.
    assert.ok(attempts >= 5, `expected ≥5 attempts (counter reset on success), got ${attempts}`);
  });
});
