import { describe, it } from "node:test";
import assert from "node:assert";
import { cleanSlackMrkdwn } from "./slack-mrkdwn.js";

describe("cleanSlackMrkdwn", () => {
  it("returns empty string for empty input", () => {
    assert.strictEqual(cleanSlackMrkdwn(""), "");
    assert.strictEqual(cleanSlackMrkdwn("", new Map()), "");
  });

  it("passes through plain text unchanged", () => {
    assert.strictEqual(cleanSlackMrkdwn("Hello world"), "Hello world");
  });

  // ── Link resolution ────────────────────────────────────────────────

  it("converts <url|label> links to label", () => {
    assert.strictEqual(
      cleanSlackMrkdwn("<https://example.com|Click here>"),
      "Click here",
    );
  });

  it("converts bare URL links", () => {
    assert.strictEqual(
      cleanSlackMrkdwn("<https://example.com>"),
      "https://example.com",
    );
  });

  it("handles real-world Slack link with @mention label", () => {
    // This is the exact pattern from the bug report
    assert.strictEqual(
      cleanSlackMrkdwn("<trailhead.com|@Tim Smith> created a new opportunity"),
      "@Tim Smith created a new opportunity",
    );
  });

  it("handles multiple links in one message", () => {
    assert.strictEqual(
      cleanSlackMrkdwn("<https://a.com|A> and <https://b.com|B>"),
      "A and B",
    );
  });

  // ── User mentions ─────────────────────────────────────────────────

  it("resolves <@U123> to @DisplayName via userMap", () => {
    const userMap = new Map([
      ["U123", { name: "Alice Chen", image: null }],
    ]);
    assert.strictEqual(
      cleanSlackMrkdwn("<@U123> said hello", userMap),
      "@Alice Chen said hello",
    );
  });

  it("falls back to userId when not in userMap", () => {
    assert.strictEqual(
      cleanSlackMrkdwn("<@U999> said hello"),
      "@U999 said hello",
    );
  });

  it("uses fallback name from <@U123|fallback>", () => {
    assert.strictEqual(
      cleanSlackMrkdwn("<@U123|bob>"),
      "@bob",
    );
  });

  it("prefers userMap over fallback name", () => {
    const userMap = new Map([
      ["U123", { name: "Alice Chen", image: null }],
    ]);
    assert.strictEqual(
      cleanSlackMrkdwn("<@U123|bob>", userMap),
      "@Alice Chen",
    );
  });

  // ── Channel mentions ──────────────────────────────────────────────

  it("converts <#C123|general> to #general", () => {
    assert.strictEqual(
      cleanSlackMrkdwn("Join <#C123|general>"),
      "Join #general",
    );
  });

  it("handles <#C123> without label", () => {
    assert.strictEqual(
      cleanSlackMrkdwn("<#C123>"),
      "#C123",
    );
  });

  // ── Special mentions ──────────────────────────────────────────────

  it("converts <!here> to @here", () => {
    assert.strictEqual(cleanSlackMrkdwn("<!here>"), "@here");
  });

  it("converts <!channel> to @channel", () => {
    assert.strictEqual(cleanSlackMrkdwn("<!channel>"), "@channel");
  });

  it("converts <!everyone> to @everyone", () => {
    assert.strictEqual(cleanSlackMrkdwn("<!everyone>"), "@everyone");
  });

  it("converts <!subteam^S123|@engineering> to @engineering", () => {
    assert.strictEqual(
      cleanSlackMrkdwn("<!subteam^S123|@engineering>"),
      "@engineering",
    );
  });

  // ── HTML entities ─────────────────────────────────────────────────

  it("decodes &amp; &lt; &gt;", () => {
    assert.strictEqual(
      cleanSlackMrkdwn("a &amp; b &lt; c &gt; d"),
      "a & b < c > d",
    );
  });

  // CodeQL js/double-escaping (alert #8): if the user literally typed
  // `&lt;`, Slack escapes the leading `&` as `&amp;`, so the message we
  // receive is `&amp;lt;`. We must decode `&lt;`/`&gt;` before `&amp;`
  // so the final pass surfaces `&lt;` as text, not `<`.
  it("does not double-decode user-typed entities", () => {
    assert.strictEqual(cleanSlackMrkdwn("&amp;lt;"), "&lt;");
    assert.strictEqual(cleanSlackMrkdwn("&amp;gt;"), "&gt;");
    assert.strictEqual(cleanSlackMrkdwn("&amp;amp;"), "&amp;");
  });

  // ── Formatting markers ────────────────────────────────────────────

  it("strips *bold* markers", () => {
    assert.strictEqual(
      cleanSlackMrkdwn("This is *bold text* here"),
      "This is bold text here",
    );
  });

  it("strips _italic_ markers", () => {
    assert.strictEqual(
      cleanSlackMrkdwn("This is _italic text_ here"),
      "This is italic text here",
    );
  });

  it("strips ~strikethrough~ markers", () => {
    assert.strictEqual(
      cleanSlackMrkdwn("This is ~deleted~ here"),
      "This is deleted here",
    );
  });

  it("preserves mid-word underscores (snake_case)", () => {
    assert.strictEqual(
      cleanSlackMrkdwn("use my_variable_name here"),
      "use my_variable_name here",
    );
  });

  // ── Newline preservation ──────────────────────────────────────────

  it("preserves newlines in list content", () => {
    const input = "Here's what you can do:\n- Collaborate\n- Share tips\n- Discuss";
    assert.strictEqual(cleanSlackMrkdwn(input), input);
  });

  // ── Combined real-world examples ──────────────────────────────────

  it("handles complex real-world message from bug report", () => {
    const input = "<trailhead.com|@Tim Smith> created a new opportunity in Sales Cloud :cloud: *<trailhead.com|Omega, Inc. - Agentforce Add-on Business>*";
    const expected = "@Tim Smith created a new opportunity in Sales Cloud :cloud: Omega, Inc. - Agentforce Add-on Business";
    assert.strictEqual(cleanSlackMrkdwn(input), expected);
  });

  it("handles blockquote with HTML entities", () => {
    const input = "&gt;Lightning Dashboard |\n&gt;In 07 - Sales Dashboard";
    const expected = "  Lightning Dashboard |\n  In 07 - Sales Dashboard";
    assert.strictEqual(cleanSlackMrkdwn(input), expected);
  });

  // ── Unpaired formatting artifacts ─────────────────────────────────

  it("strips leading * bullet remnants", () => {
    assert.strictEqual(
      cleanSlackMrkdwn("* Stage: Qualification\n* Close Date: 2025-06-30"),
      "Stage: Qualification\nClose Date: 2025-06-30",
    );
  });

  it("strips double ** from collapsed bold markers", () => {
    assert.strictEqual(
      cleanSlackMrkdwn("swap **Omega, Inc."),
      "swap Omega, Inc.",
    );
  });

  it("strips trailing lone *", () => {
    assert.strictEqual(
      cleanSlackMrkdwn("some text *"),
      "some text",
    );
  });
});
