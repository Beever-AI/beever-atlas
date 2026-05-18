import { describe, it } from "node:test";
import assert from "node:assert";
import {
  recordTeamsConversation,
  recordTelegramChat,
  pruneStaleTeamsConversations,
  pruneStaleTelegramChats,
  clearUserProfileCache,
  clearMattermostUserCache,
} from "./bridge.js";

// ── Teams conversation registry prune (RES-286) ──────────────────────────────

describe("pruneStaleTeamsConversations", () => {
  it("returns 0 when registry is empty", () => {
    // Make sure prior tests don't bleed in — empty the registry via a huge
    // maxAge=0 (drops everything older than `now`).
    pruneStaleTeamsConversations(0);
    assert.strictEqual(pruneStaleTeamsConversations(0), 0);
  });

  it("does not prune entries within the maxAge window", () => {
    pruneStaleTeamsConversations(0); // clear

    recordTeamsConversation("conn-x", {
      conversation: { id: "fresh-conv", conversationType: "channel" },
      channelData: { team: { id: "t1", name: "TeamA" }, channel: { id: "c1", name: "general" } },
    });

    // 1-day maxAge: a just-inserted entry must NOT be pruned.
    assert.strictEqual(pruneStaleTeamsConversations(24 * 60 * 60 * 1000), 0);
    pruneStaleTeamsConversations(0); // tidy
  });

  it("removes empty connection buckets after prune", () => {
    pruneStaleTeamsConversations(0); // clear all
    recordTeamsConversation("conn-empty-after-prune", {
      conversation: { id: "only", conversationType: "channel" },
      channelData: { team: { name: "T" }, channel: { name: "c" } },
    });
    // Prune with maxAge=0 ms drops everything ≥1ms old. Wait 5ms to ensure.
    return new Promise<void>((resolve) => {
      setTimeout(() => {
        const pruned = pruneStaleTeamsConversations(0);
        assert.ok(pruned >= 1, `expected ≥1 pruned, got ${pruned}`);
        // Subsequent prune is a no-op.
        assert.strictEqual(pruneStaleTeamsConversations(0), 0);
        resolve();
      }, 5);
    });
  });
});

// ── Telegram chat registry prune (RES-286) ───────────────────────────────────

describe("pruneStaleTelegramChats", () => {
  it("returns 0 when registry is empty", () => {
    pruneStaleTelegramChats(0); // empty it
    assert.strictEqual(pruneStaleTelegramChats(0), 0);
  });

  it("ages out stale chats and keeps recent ones", () => {
    pruneStaleTelegramChats(0); // clear

    recordTelegramChat("conn-tg", {
      id: 12345,
      title: "Recent group",
      type: "group",
    });

    // Wait 5 ms then prune anything older than now (maxAge=0). The single
    // entry is at least 5 ms old.
    return new Promise<void>((resolve) => {
      setTimeout(() => {
        const pruned = pruneStaleTelegramChats(0);
        assert.ok(pruned >= 1);
        // After prune the bucket should be empty; next call is a no-op.
        assert.strictEqual(pruneStaleTelegramChats(0), 0);
        resolve();
      }, 5);
    });
  });

  it("does not prune entries within the maxAge window", () => {
    pruneStaleTelegramChats(0); // clear
    recordTelegramChat("conn-tg-2", {
      id: 67890,
      title: "Active group",
      type: "supergroup",
    });
    // maxAge of 1 day: the just-inserted entry should NOT be pruned.
    assert.strictEqual(pruneStaleTelegramChats(24 * 60 * 60 * 1000), 0);
    pruneStaleTelegramChats(0); // tidy
  });
});

// ── Cache clearers ───────────────────────────────────────────────────────────

describe("clearUserProfileCache / clearMattermostUserCache", () => {
  it("are idempotent and safe to call when caches are empty", () => {
    // We can't easily inject entries because the caches are module-private,
    // but calling the clears must never throw and must be cheap.
    assert.doesNotThrow(() => clearUserProfileCache());
    assert.doesNotThrow(() => clearUserProfileCache());
    assert.doesNotThrow(() => clearMattermostUserCache());
    assert.doesNotThrow(() => clearMattermostUserCache());
  });
});
