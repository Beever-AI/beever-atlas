import { describe, it, beforeEach, afterEach } from "node:test";
import assert from "node:assert";
import {
  DiscordGatewaySupervisor,
  resolveManagedRoleIds,
  type DiscordGatewayAdapter,
} from "./discord-gateway.js";

const tick = (ms = 5) => new Promise((r) => setTimeout(r, ms));

/** Minimal ChatManager stand-in exposing only what the supervisor consumes. */
function fakeChatManager(opts: {
  discord?: { connectionId: string; adapter: DiscordGatewayAdapter }[];
  configs?: Record<string, Record<string, string>>;
}) {
  return {
    getAdaptersByPlatform(platform: string) {
      if (platform !== "discord") return [];
      return (opts.discord ?? []).map((d) => ({
        compositeKey: `discord:${d.connectionId}`,
        connectionId: d.connectionId,
        adapter: d.adapter,
      }));
    },
    getAdapterConfig(connectionId: string) {
      return opts.configs?.[connectionId] ?? null;
    },
  } as unknown as ConstructorParameters<typeof DiscordGatewaySupervisor>[0];
}

/** Fake adapter that records startGatewayListener calls and never self-completes
 *  its listener (so the loop parks after one arming until aborted). */
function fakeAdapter() {
  const calls: { durationMs: number; webhookUrl?: string; signal: AbortSignal }[] = [];
  const adapter: DiscordGatewayAdapter & { calls: typeof calls } = {
    mentionRoleIds: [],
    calls,
    async startGatewayListener(options, durationMs, signal, webhookUrl) {
      calls.push({ durationMs, webhookUrl, signal });
      options.waitUntil(new Promise<void>(() => {})); // never resolves
      return { status: 200 };
    },
  };
  return adapter;
}

describe("DiscordGatewaySupervisor", () => {
  it("is a complete no-op when no Discord adapter is registered", async () => {
    const cm = fakeChatManager({ discord: [] });
    const sup = new DiscordGatewaySupervisor(cm, { enabled: true });
    sup.sync();
    await tick();
    assert.strictEqual(sup.activeCount(), 0);
    sup.stop();
  });

  it("starts one in-process keep-alive loop per Discord connection (no webhookUrl)", async () => {
    const a = fakeAdapter();
    const cm = fakeChatManager({ discord: [{ connectionId: "conn-1", adapter: a }] });
    const sup = new DiscordGatewaySupervisor(cm, { enabled: true, windowMs: 1234 });
    sup.sync();
    await tick();
    assert.strictEqual(sup.activeCount(), 1);
    assert.strictEqual(a.calls.length, 1);
    // In-process dispatch: no webhookUrl is passed (preserves thread context).
    assert.strictEqual(a.calls[0].webhookUrl, undefined);
    assert.strictEqual(a.calls[0].durationMs, 1234);
    assert.strictEqual(a.calls[0].signal.aborted, false);
    sup.stop();
  });

  it("retires loops bound to stale adapters on re-sync (generation guard)", async () => {
    const a = fakeAdapter();
    const cm = fakeChatManager({ discord: [{ connectionId: "conn-1", adapter: a }] });
    const sup = new DiscordGatewaySupervisor(cm, { enabled: true });
    sup.sync();
    await tick();
    const firstSignal = a.calls[0].signal;
    assert.strictEqual(firstSignal.aborted, false);

    sup.sync(); // rebuild — the previous loop must be aborted
    await tick();
    assert.strictEqual(firstSignal.aborted, true);
    sup.stop();
  });

  it("stop() aborts every active listener", async () => {
    const a = fakeAdapter();
    const cm = fakeChatManager({ discord: [{ connectionId: "conn-1", adapter: a }] });
    const sup = new DiscordGatewaySupervisor(cm, { enabled: true });
    sup.sync();
    await tick();
    const sig = a.calls[0].signal;
    sup.stop();
    assert.strictEqual(sig.aborted, true);
    assert.strictEqual(sup.activeCount(), 0);
  });

  it("does not start any loop when disabled", async () => {
    const a = fakeAdapter();
    const cm = fakeChatManager({ discord: [{ connectionId: "conn-1", adapter: a }] });
    const sup = new DiscordGatewaySupervisor(cm, { enabled: false });
    sup.sync();
    await tick();
    assert.strictEqual(a.calls.length, 0);
    assert.strictEqual(sup.activeCount(), 0);
  });

  it("re-arms (does not crash) when a window ends abnormally fast", async () => {
    const calls: number[] = [];
    let aborted = false;
    const adapter: DiscordGatewayAdapter = {
      mentionRoleIds: [],
      async startGatewayListener(options, _durationMs, signal) {
        calls.push(Date.now());
        signal.addEventListener("abort", () => { aborted = true; }, { once: true });
        options.waitUntil(Promise.resolve()); // completes immediately → must be paced
        return { status: 500 }; // also exercises the >=400 backoff path
      },
    };
    const cm = fakeChatManager({ discord: [{ connectionId: "c", adapter }] });
    const sup = new DiscordGatewaySupervisor(cm, { enabled: true, retryMs: 20 });
    sup.sync();
    await tick(60); // long enough for ~2 paced re-arms, not a hot loop
    sup.stop();
    await tick(40);
    assert.ok(calls.length >= 1, "armed at least once");
    assert.ok(calls.length <= 5, `paced, not a hot loop (saw ${calls.length})`);
    assert.strictEqual(aborted, true);
  });

  it("resolves and merges the bot's managed role into mentionRoleIds", async () => {
    const a = fakeAdapter();
    a.mentionRoleIds = ["existing-role"];
    const cm = fakeChatManager({
      discord: [{ connectionId: "conn-1", adapter: a }],
      configs: { "conn-1": { botToken: "tok", applicationId: "APP123" } },
    });
    const orig = globalThis.fetch;
    globalThis.fetch = (async (url: string) => {
      if (url.endsWith("/users/@me/guilds")) {
        return { ok: true, json: async () => [{ id: "guild-1" }] } as Response;
      }
      if (url.includes("/guilds/guild-1/roles")) {
        return {
          ok: true,
          json: async () => [
            { id: "managed-role", managed: true, tags: { bot_id: "APP123" } },
            { id: "other-bot-role", managed: true, tags: { bot_id: "OTHER" } },
            { id: "plain-role", managed: false },
          ],
        } as Response;
      }
      return { ok: false, json: async () => [] } as Response;
    }) as typeof fetch;
    try {
      const sup = new DiscordGatewaySupervisor(cm, { enabled: true });
      sup.sync();
      await tick(20);
      assert.deepStrictEqual(a.mentionRoleIds, ["existing-role", "managed-role"]);
      sup.stop();
    } finally {
      globalThis.fetch = orig;
    }
  });

  it("caches managed roles across re-syncs (no repeat Discord calls)", async () => {
    const a = fakeAdapter();
    const cm = fakeChatManager({
      discord: [{ connectionId: "conn-1", adapter: a }],
      configs: { "conn-1": { botToken: "tok", applicationId: "APP123" } },
    });
    const orig = globalThis.fetch;
    let guildCalls = 0;
    globalThis.fetch = (async (url: string) => {
      if (url.endsWith("/users/@me/guilds")) {
        guildCalls += 1;
        return { ok: true, json: async () => [{ id: "guild-1" }] } as Response;
      }
      if (url.includes("/guilds/guild-1/roles")) {
        return {
          ok: true,
          json: async () => [{ id: "managed-role", managed: true, tags: { bot_id: "APP123" } }],
        } as Response;
      }
      return { ok: false, json: async () => [] } as Response;
    }) as typeof fetch;
    try {
      const sup = new DiscordGatewaySupervisor(cm, { enabled: true });
      sup.sync();
      await tick(20);
      sup.sync(); // simulate a rebuild/recycle
      await tick(20);
      assert.strictEqual(guildCalls, 1, "managed-role REST resolved once, then cached");
      assert.deepStrictEqual(a.mentionRoleIds, ["managed-role"]);
      sup.stop();
    } finally {
      globalThis.fetch = orig;
    }
  });
});

describe("resolveManagedRoleIds", () => {
  const orig = globalThis.fetch;
  afterEach(() => { globalThis.fetch = orig; });
  beforeEach(() => { globalThis.fetch = orig; });

  it("returns only managed roles whose tags.bot_id matches the application", async () => {
    globalThis.fetch = (async (url: string) => {
      if (url.endsWith("/users/@me/guilds")) {
        return { ok: true, json: async () => [{ id: "g1" }, { id: "g2" }] } as Response;
      }
      if (url.includes("/guilds/g1/roles")) {
        return {
          ok: true,
          json: async () => [
            { id: "r-match", managed: true, tags: { bot_id: "APP" } },
            { id: "r-nomanage", managed: false, tags: { bot_id: "APP" } },
          ],
        } as Response;
      }
      if (url.includes("/guilds/g2/roles")) {
        return {
          ok: true,
          json: async () => [{ id: "r-otherbot", managed: true, tags: { bot_id: "ZZZ" } }],
        } as Response;
      }
      return { ok: false, json: async () => [] } as Response;
    }) as typeof fetch;
    const roles = await resolveManagedRoleIds("tok", "APP");
    assert.deepStrictEqual(roles, ["r-match"]);
  });

  it("returns [] when the guilds call fails (best-effort, never throws)", async () => {
    globalThis.fetch = (async () => ({ ok: false, json: async () => [] }) as Response) as typeof fetch;
    const roles = await resolveManagedRoleIds("tok", "APP");
    assert.deepStrictEqual(roles, []);
  });

  it("returns [] and does not throw when fetch rejects", async () => {
    globalThis.fetch = (async () => { throw new Error("network down"); }) as typeof fetch;
    const roles = await resolveManagedRoleIds("tok", "APP");
    assert.deepStrictEqual(roles, []);
  });
});
