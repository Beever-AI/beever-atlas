import { describe, it } from "node:test";
import assert from "node:assert";
import { extractChannelId, extractPlatform, hasThreadRoot } from "./thread-id.js";

describe("extractChannelId", () => {
  it("returns the second segment", () => {
    assert.strictEqual(extractChannelId("slack:C123:1700000000.0001"), "C123");
    assert.strictEqual(extractChannelId("discord:guild456"), "guild456");
  });

  it("falls back to the whole id when unsegmented", () => {
    assert.strictEqual(extractChannelId("nocolons"), "nocolons");
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
  });

  it("is false for an id with no (or empty) thread segment", () => {
    assert.strictEqual(hasThreadRoot("slack:C123"), false);
    assert.strictEqual(hasThreadRoot("slack:C123:"), false);
    assert.strictEqual(hasThreadRoot("nocolons"), false);
  });
});
