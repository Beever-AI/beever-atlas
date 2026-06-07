import { describe, it } from "node:test";
import assert from "node:assert";
import { extractChannelId, extractThreadId, extractPlatform, hasThreadRoot } from "./thread-id.js";

describe("extractChannelId", () => {
  it("returns the second segment for non-Discord platforms", () => {
    assert.strictEqual(extractChannelId("slack:C123:1700000000.0001"), "C123");
    assert.strictEqual(extractChannelId("teams:19:abc"), "19");
    assert.strictEqual(extractChannelId("telegram:-100123:7"), "-100123");
  });

  it("returns the THIRD segment for Discord (skips the leading guild id)", () => {
    // discord:<guildId>:<channelId>:<thread>
    assert.strictEqual(
      extractChannelId("discord:1507110330390806652:1507110962748981372:1513020110724661259"),
      "1507110962748981372",
    );
    // discord:<guildId>:<channelId> (no thread)
    assert.strictEqual(extractChannelId("discord:guild1:chan1"), "chan1");
  });

  it("falls back gracefully for short/unsegmented ids", () => {
    assert.strictEqual(extractChannelId("nocolons"), "nocolons");
    assert.strictEqual(extractChannelId("discord:guild456"), "guild456");
  });
});

describe("extractThreadId", () => {
  it("returns everything after the channel for non-Discord platforms", () => {
    assert.strictEqual(extractThreadId("slack:C123:1700000000.0001"), "1700000000.0001");
  });

  it("returns the thread after the guild+channel for Discord", () => {
    assert.strictEqual(
      extractThreadId("discord:1507110330390806652:1507110962748981372:1513020110724661259"),
      "1513020110724661259",
    );
  });

  it("falls back to the whole id when Discord has no thread token", () => {
    assert.strictEqual(extractThreadId("discord:guild1:chan1"), "discord:guild1:chan1");
  });
});

describe("extractPlatform", () => {
  it("returns the lower-cased platform prefix", () => {
    assert.strictEqual(extractPlatform("slack:C123:ts"), "slack");
    assert.strictEqual(extractPlatform("Discord:g1"), "discord");
    assert.strictEqual(extractPlatform("teams:19:abc"), "teams");
    assert.strictEqual(extractPlatform("mattermost:chan"), "mattermost");
    assert.strictEqual(extractPlatform("telegram:-100123"), "telegram");
  });

  it("returns 'unknown' for malformed ids", () => {
    assert.strictEqual(extractPlatform("nocolons"), "unknown");
    assert.strictEqual(extractPlatform(":emptyprefix"), "unknown");
  });
});

describe("hasThreadRoot", () => {
  it("is true for an id with a non-empty thread segment", () => {
    assert.strictEqual(hasThreadRoot("slack:C123:1700000000.0001"), true);
    assert.strictEqual(hasThreadRoot("teams:19:abc"), true);
    // Discord needs a 4th token (after guild+channel) to count as a thread root.
    assert.strictEqual(hasThreadRoot("discord:guild1:chan1:thread1"), true);
  });

  it("is false for an id with no (or empty) thread segment", () => {
    assert.strictEqual(hasThreadRoot("slack:C123"), false);
    assert.strictEqual(hasThreadRoot("slack:C123:"), false);
    assert.strictEqual(hasThreadRoot("nocolons"), false);
    // Discord channel mention with no thread yet — not a thread root.
    assert.strictEqual(hasThreadRoot("discord:guild1:chan1"), false);
    assert.strictEqual(hasThreadRoot("discord:guild1:chan1:"), false);
  });
});
