import { describe, it } from "node:test";
import assert from "node:assert";
import { stripMention } from "./mentions.js";

describe("stripMention", () => {
  it("strips Slack mentions (with and without label)", () => {
    assert.strictEqual(stripMention("<@U123> what is our stack?", "slack"), "what is our stack?");
    assert.strictEqual(stripMention("<@U123|beever> hi", "slack"), "hi");
  });

  it("strips Discord user, nickname, and role mentions", () => {
    assert.strictEqual(stripMention("<@123456> question", "discord"), "question");
    assert.strictEqual(stripMention("<@!123456> question", "discord"), "question");
    assert.strictEqual(stripMention("<@&987> question", "discord"), "question");
  });

  it("strips Teams <at> mentions", () => {
    assert.strictEqual(stripMention("<at>Beever Atlas</at> what's new?", "teams"), "what's new?");
  });

  it("strips a leading @handle on Mattermost/Telegram only", () => {
    assert.strictEqual(stripMention("@beever what's up", "mattermost"), "what's up");
    assert.strictEqual(stripMention("@beever_bot hello", "telegram"), "hello");
  });

  it("does NOT strip a leading @handle on Slack (bracket-only platform)", () => {
    assert.strictEqual(stripMention("@channel please answer", "slack"), "@channel please answer");
  });

  it("leaves a mid-sentence @handle intact on Mattermost", () => {
    assert.strictEqual(stripMention("please ping @bob about it", "mattermost"), "please ping @bob about it");
  });

  it("strips only the leading bot @handle, keeping a following @user", () => {
    assert.strictEqual(stripMention("@beever @bob what happened", "mattermost"), "@bob what happened");
  });

  it("returns empty string for a mention-only message", () => {
    assert.strictEqual(stripMention("<@U123>", "slack"), "");
    assert.strictEqual(stripMention("   ", "slack"), "");
  });

  it("leaves plain text untouched", () => {
    assert.strictEqual(stripMention("what is our roadmap?", "discord"), "what is our roadmap?");
  });
});
