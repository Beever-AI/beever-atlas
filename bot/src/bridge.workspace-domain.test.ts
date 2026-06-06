/**
 * attachSlackWorkspaceDomain — the helper every single-channel bridge route uses
 * to stamp the Slack workspace domain onto a channel so the backend can build
 * clickable citation permalinks.
 *
 * Regression guard for the bug live-testing caught: the backend resolves
 * channels via the PER-CONNECTION route, so patching only the platform-level
 * route left permalinks dead. The helper centralizes the logic; these tests pin
 * the exact-by-connection vs platform-fallback behavior.
 */
import { describe, it } from "node:test";
import assert from "node:assert";
import { attachSlackWorkspaceDomain } from "./bridge.js";
import type { NormalizedChannel } from "./bridge.js";

function slackChannel(over: Partial<NormalizedChannel> = {}): NormalizedChannel {
  return {
    channel_id: "C1",
    name: "general",
    platform: "slack",
    is_member: true,
    member_count: null,
    topic: null,
    purpose: null,
    ...over,
  };
}

/** Minimal ChatManager stub exposing only the two domain getters the helper calls. */
function stubManager(byConn: Record<string, string>, byPlatform: string | null) {
  return {
    getWorkspaceDomain: (id: string) => byConn[id] ?? null,
    getWorkspaceDomainForPlatform: (_p: string) => byPlatform,
  } as unknown as Parameters<typeof attachSlackWorkspaceDomain>[1];
}

describe("attachSlackWorkspaceDomain", () => {
  it("uses the EXACT per-connection domain when a connectionId is given", () => {
    const ch = slackChannel();
    attachSlackWorkspaceDomain(ch, stubManager({ "conn-1": "beeveratlas" }, "other"), "conn-1");
    assert.strictEqual(ch.workspace_domain, "beeveratlas");
  });

  it("falls back to the platform domain when the connection has none", () => {
    const ch = slackChannel();
    attachSlackWorkspaceDomain(ch, stubManager({}, "beeveratlas"), "conn-unknown");
    assert.strictEqual(ch.workspace_domain, "beeveratlas");
  });

  it("uses the platform domain when no connectionId is given", () => {
    const ch = slackChannel();
    attachSlackWorkspaceDomain(ch, stubManager({}, "beeveratlas"));
    assert.strictEqual(ch.workspace_domain, "beeveratlas");
  });

  it("is a no-op for non-Slack channels", () => {
    const ch = slackChannel({ platform: "discord" });
    attachSlackWorkspaceDomain(ch, stubManager({ "conn-1": "beeveratlas" }, "beeveratlas"), "conn-1");
    assert.strictEqual(ch.workspace_domain, undefined);
  });

  it("never overwrites a domain the channel already carries", () => {
    const ch = slackChannel({ workspace_domain: "preset" });
    attachSlackWorkspaceDomain(ch, stubManager({ "conn-1": "beeveratlas" }, "beeveratlas"), "conn-1");
    assert.strictEqual(ch.workspace_domain, "preset");
  });

  it("leaves the domain unset when none is known anywhere", () => {
    const ch = slackChannel();
    attachSlackWorkspaceDomain(ch, stubManager({}, null), "conn-1");
    assert.strictEqual(ch.workspace_domain, undefined);
  });
});
