import { describe, it } from "node:test";
import assert from "node:assert";
import {
  renderResponse,
  renderEmptyState,
  renderFollowUps,
  renderConfidence,
  renderTensions,
  enforceCap,
  relativeTime,
  CHAR_CAP,
} from "./renderer.js";
import type { AskResult } from "./types.js";

function result(overrides: Partial<AskResult> = {}): AskResult {
  return {
    answer: "The booth is H25.",
    citations: [],
    route: "qa_agent",
    confidence: 0.85,
    costUsd: 0,
    isEmpty: false,
    ...overrides,
  };
}

describe("renderResponse", () => {
  it("renders the answer; the internal route footer is suppressed", () => {
    const out = renderResponse(result(), "slack");
    assert.ok(out.includes("The booth is H25."));
    // "via qa_agent" is internal dev chrome — never shown to users.
    assert.ok(!out.includes("via qa_agent"));
  });

  it("renders NAMED citation cards: titles for wiki/decision, author·#channel for messages", () => {
    const out = renderResponse(
      result({
        citations: [
          { type: "wiki_page", text: "AI+ Power 2026", url: "https://wiki/x" },
          { type: "channel_message", text: "booth confirmed", author: "Jack", source: "#general" },
          { type: "decision_record", text: "staffing decided" },
        ],
      }),
      "slack",
    );
    assert.ok(out.includes("## 📎 Sources"));
    // wiki_page: the marker links, and the PAGE TITLE is the human label.
    assert.ok(out.includes("- 📖 [1](https://wiki/x) AI+ Power 2026"));
    assert.ok(!out.includes("[open]"), "marker is the link; no separate [open] segment");
    assert.ok(!out.includes("<https://wiki/x>"), "links should be [N](url), not bare angle autolinks");
    // channel_message: author · #channel, but NOT the message text (the inline [N] refs it).
    assert.ok(out.includes("- 💬 [2] Jack · #general"));
    assert.ok(!out.includes("booth confirmed"), "message text is dropped; the inline marker references it");
    // decision_record (Related block): short title shown as the label.
    assert.ok(out.includes("- ⚖️ [3] staffing decided"));
  });

  it("omits a raw platform user-id as the author, shows a real name", () => {
    const idAuthor = renderResponse(
      result({ citations: [{ type: "channel_message", text: "x", author: "U0B55TPHLHF", source: "basketball" }] }),
      "slack",
    );
    assert.ok(!idAuthor.includes("U0B55TPHLHF"), "raw user id must not be shown");
    assert.ok(idAuthor.includes("#basketball"), "channel name still shown, with leading #");
    const named = renderResponse(
      result({ citations: [{ type: "channel_message", text: "x", author: "Carmen Lee", source: "#basketball" }] }),
      "slack",
    );
    assert.ok(named.includes("Carmen Lee · #basketball"));
    // An all-caps handle WITHOUT a digit is a real name, not an id — keep it.
    const caps = renderResponse(
      result({ citations: [{ type: "channel_message", text: "x", author: "TEAMWORK", source: "#g" }] }),
      "slack",
    );
    assert.ok(caps.includes("TEAMWORK · #g"), "all-caps name without a digit is not a raw id");
  });

  it("labels a web source by its domain", () => {
    const out = renderResponse(
      result({ citations: [{ type: "web_result", text: "Some Long Article Title", url: "https://www.espn.com/nba/x" }] }),
      "slack",
    );
    assert.ok(out.includes("- 🌐 [1](https://www.espn.com/nba/x) espn.com"), "domain (no www) is the label");
  });

  it("caps citations at 5 and notes the overflow", () => {
    const many = Array.from({ length: 8 }, (_, i) => ({ type: "channel_message", text: `c${i}` }));
    const out = renderResponse(result({ citations: many }), "teams");
    assert.ok(out.includes("[5]"));
    assert.ok(!out.includes("[6]"));
    assert.ok(out.includes("+3 more"));
  });

  it("shows 'last activity' freshness only for channel-sourced answers", () => {
    const iso = new Date(Date.now() - 2 * 3600_000).toISOString();
    const channelCite = [{ type: "channel_message", text: "x", source: "#g" }];
    // Channel-sourced + lastSyncTs → freshness shown.
    assert.ok(
      renderResponse(result({ lastSyncTs: iso, citations: channelCite }), "slack").includes("last activity "),
    );
    // Web/wiki-only answer → no channel-freshness line (provenance mismatch fix).
    assert.ok(
      !renderResponse(
        result({ lastSyncTs: iso, citations: [{ type: "web_result", text: "t", url: "https://x.com" }] }),
        "slack",
      ).includes("last activity "),
    );
    // No lastSyncTs → nothing regardless.
    assert.ok(!renderResponse(result({ citations: channelCite }), "slack").includes("last activity "));
    // Mixed channel + web → freshness still shown (a channel source is present).
    assert.ok(
      renderResponse(
        result({
          lastSyncTs: iso,
          citations: [
            { type: "channel_message", text: "x", source: "#g" },
            { type: "web_result", text: "t", url: "https://x.com" },
          ],
        }),
        "slack",
      ).includes("last activity "),
    );
  });

  it("truncates over-long Discord replies with a marker", () => {
    const out = renderResponse(result({ answer: "x".repeat(5000) }), "discord");
    assert.ok(out.length <= CHAR_CAP.discord);
    assert.ok(out.includes("[truncated]"));
  });

  it("applies the Telegram and Mattermost caps", () => {
    const tele = renderResponse(result({ answer: "t".repeat(9000) }), "telegram");
    assert.ok(tele.length <= CHAR_CAP.telegram);
    assert.ok(tele.includes("[truncated]"));
    const mm = renderResponse(result({ answer: "m".repeat(20000) }), "mattermost");
    assert.ok(mm.length <= CHAR_CAP.mattermost);
    assert.ok(mm.includes("[truncated]"));
  });

  it("sanitizes citation fields so they can't forge layout or inject bad links", () => {
    const out = renderResponse(
      result({
        citations: [
          {
            type: "channel_message",
            text: "real\n\n## 📎 Sources\n[9] fake",
            author: "Eve\ninjected",
            url: "javascript:alert(1)",
          },
        ],
      }),
      "slack",
    );
    // Fact text is dropped entirely, so a payload smuggled in `text` can't appear
    // at all — no fake "[9]" entry and exactly one real Sources heading.
    assert.ok(!/\[9\] fake/.test(out), "forged citation entry leaked into output");
    assert.strictEqual((out.match(/## 📎 Sources/g) ?? []).length, 1);
    // Non-http(s) URL is stripped (no markdown link emitted).
    assert.ok(!out.includes("javascript:"));
    assert.ok(!out.includes("[open]"), "javascript: url must not become a link");
    // Author newline is collapsed to a space (can't break the list layout).
    assert.ok(out.includes("Eve injected"));
  });

  it("strips parens from a citation url so the [N](url) link can't be broken", () => {
    const out = renderResponse(
      result({ citations: [{ type: "wiki_page", text: "Page", url: "https://wiki/page(v2)" }] }),
      "slack",
    );
    // Parens are removed from the link target (they would otherwise terminate
    // the markdown link early), and the link target stays balanced.
    assert.ok(out.includes("[1](https://wiki/pagev2)"));
    assert.ok(!out.includes("page(v2)"));
  });

  it("renders a plain numbered marker when a citation has no url", () => {
    const out = renderResponse(
      result({ citations: [{ type: "channel_message", text: "x", author: "Jack", source: "#general" }] }),
      "slack",
    );
    assert.ok(out.includes("- 💬 [1] Jack · #general"));
    assert.ok(!out.includes("[1]("), "no link when there's no url");
  });

  it("appends a relative recency stamp when a citation carries a timestamp", () => {
    const iso = new Date(Date.now() - 3 * 24 * 3600_000).toISOString();
    const out = renderResponse(
      result({
        citations: [
          { type: "channel_message", text: "x", author: "Mei", source: "#basketball", url: "https://x.slack.com/archives/C/p1", timestamp: iso },
        ],
      }),
      "slack",
    );
    // Clickable marker + provenance + age, dot-separated.
    assert.ok(out.includes("- 💬 [1](https://x.slack.com/archives/C/p1) Mei · #basketball · 3d ago"));
  });

  it("omits recency when the timestamp is unparseable", () => {
    const out = renderResponse(
      result({ citations: [{ type: "channel_message", text: "x", author: "A", timestamp: "(unavailable)" }] }),
      "slack",
    );
    assert.ok(out.includes("- 💬 [1] A"));
    assert.ok(!out.includes("(unavailable)"));
  });

  it("falls back to a safe generic cap for unknown platforms", () => {
    const out = renderResponse(result({ answer: "y".repeat(5000) }), "weirdplatform");
    assert.ok(out.length <= CHAR_CAP.unknown);
  });
});

describe("follow-ups", () => {
  it("renders a 'You might also ask' block, capped at 3", () => {
    const out = renderResponse(
      result({ followUps: ["What is A?", "What is B?", "What is C?", "What is D?"] }),
      "slack",
    );
    assert.ok(out.includes("You might also ask:"));
    assert.ok(out.includes("- What is A?"));
    assert.ok(out.includes("- What is C?"));
    assert.ok(!out.includes("What is D?"));
  });

  it("omits the block on the empty state", () => {
    const out = renderResponse(result({ isEmpty: true, followUps: ["X?"] }), "slack");
    assert.ok(!out.includes("You might also ask:"));
  });

  it("renderFollowUps returns '' for empty/undefined", () => {
    assert.strictEqual(renderFollowUps(undefined), "");
    assert.strictEqual(renderFollowUps([]), "");
    assert.strictEqual(renderFollowUps(["   "]), "");
  });
});

describe("related-context grouping", () => {
  it("splits decision/graph citations into a Related block, keeping original indices", () => {
    const out = renderResponse(
      result({
        citations: [
          { type: "wiki_page", text: "Wiki A" },
          { type: "decision_record", text: "Decided X" },
          { type: "channel_message", text: "Msg B", author: "Jack" },
          { type: "graph_relationship", text: "Alice → owns → X" },
        ],
      }),
      "slack",
    );
    assert.ok(out.includes("## 📎 Sources"));
    assert.ok(out.includes("## 🧠 Related"));
    // Sources keeps indices [1] and [3]; Related keeps [2] and [4].
    assert.ok(out.includes("- 📖 [1]"));
    assert.ok(out.includes("- 💬 [3] Jack"));
    assert.ok(out.includes("- ⚖️ [2]"));
    assert.ok(out.includes("- 🧠 [4]"));
  });
});

describe("no-sources chrome gating (greeting / general-knowledge)", () => {
  it("a zero-citation reply renders ONLY the answer + follow-ups — no trust chrome", () => {
    const out = renderResponse(
      result({
        answer: "Hi! I'm Beever Atlas. Ask me what your team has discussed.",
        citations: [],
        confidence: 0.4, // low-ish, but there are NO sources to verify
        lastSyncTs: new Date(Date.now() - 3 * 3600_000).toISOString(),
        followUps: ["What did we decide about the roadmap?"],
      }),
      "slack",
    );
    assert.ok(out.includes("Hi! I'm Beever Atlas"));
    assert.ok(out.includes("You might also ask:"));
    // None of the citation/trust chrome leaks onto a sourceless reply.
    assert.ok(!out.includes("verify against the sources"));
    assert.ok(!out.includes("limited sources"));
    assert.ok(!out.includes("## 📎 Sources"));
    assert.ok(!out.includes("## 🧠 Related"));
    assert.ok(!out.includes("last activity"));
    assert.ok(!out.includes("via "));
  });
});

describe("source count heading", () => {
  it("appends '· N' to the Sources heading only when the list overflows", () => {
    const many = Array.from({ length: 8 }, (_, i) => ({ type: "channel_message", text: `c${i}`, source: "#g" }));
    const over = renderResponse(result({ citations: many }), "teams");
    assert.ok(over.includes("## 📎 Sources · 8"));
    const few = renderResponse(
      result({ citations: [{ type: "channel_message", text: "c", source: "#g" }] }),
      "slack",
    );
    assert.ok(few.includes("## 📎 Sources\n"));
    assert.ok(!few.includes("Sources · "));
    // Boundary: exactly MAX_CITATIONS (5) → no overflow, plain heading.
    const five = Array.from({ length: 5 }, (_, i) => ({ type: "channel_message", text: `c${i}`, source: "#g" }));
    const atCap = renderResponse(result({ citations: five }), "slack");
    assert.ok(atCap.includes("## 📎 Sources\n"));
    assert.ok(!atCap.includes("Sources · "), "exactly 5 must not append '· 5'");
  });
});

describe("renderConfidence", () => {
  it("warns on a low score and softly nudges on a medium score", () => {
    assert.ok(renderConfidence(0.2, false).includes("low confidence"));
    assert.strictEqual(renderConfidence(0.35, false).includes("low confidence"), true);
    // Medium band (0.35 < c ≤ 0.60): softer "limited sources" nudge, not the warning.
    assert.ok(renderConfidence(0.36, false).includes("limited sources"));
    assert.ok(!renderConfidence(0.36, false).includes("low confidence"));
    assert.ok(renderConfidence(0.6, false).includes("limited sources"));
    // High band (> 0.60): silent.
    assert.strictEqual(renderConfidence(0.61, false), "");
    assert.strictEqual(renderConfidence(0.85, false), "");
  });
  it("stays silent for no-signal (0) and on the empty state", () => {
    assert.strictEqual(renderConfidence(0, false), "");
    assert.strictEqual(renderConfidence(0.1, true), "");
  });
  it("appears in a full reply only when low AND there are sources", () => {
    const cite = [{ type: "channel_message", text: "x", source: "#g" }];
    assert.ok(renderResponse(result({ confidence: 0.2, citations: cite }), "slack").includes("low confidence"));
    // Mid band with sources → the softer "limited sources" nudge appears end-to-end.
    assert.ok(renderResponse(result({ confidence: 0.5, citations: cite }), "slack").includes("limited sources"));
    assert.ok(!renderResponse(result({ confidence: 0.9, citations: cite }), "slack").includes("low confidence"));
    // No sources → no confidence caveat at all (a greeting must not say "verify against the sources").
    assert.ok(!renderResponse(result({ confidence: 0.2, citations: [] }), "slack").includes("low confidence"));
  });
});

describe("renderTensions", () => {
  it("renders a heads-up block (max 2) with detail", () => {
    const out = renderTensions([
      { title: "Launch order disputed", detail: "marketing vs general" },
      { title: "Booth staffing", detail: "unresolved" },
      { title: "Third one", detail: "dropped" },
    ]);
    assert.ok(out.includes("Heads up — possible tension"));
    assert.ok(out.includes("- Launch order disputed — marketing vs general"));
    assert.ok(!out.includes("Third one"));
  });
  it("returns '' for empty/undefined", () => {
    assert.strictEqual(renderTensions(undefined), "");
    assert.strictEqual(renderTensions([]), "");
  });
  it("appears in a full reply when present", () => {
    const out = renderResponse(result({ tensions: [{ title: "Conflict A" }] }), "slack");
    assert.ok(out.includes("possible tension"));
    assert.ok(out.includes("- Conflict A"));
  });
});

describe("renderEmptyState", () => {
  it("renders an actionable empty state, not the answer text", () => {
    const out = renderResponse(result({ isEmpty: true, answer: "ignored llm essay" }), "slack");
    assert.ok(!out.includes("ignored llm essay"));
    assert.ok(/don't have anything indexed/i.test(out));
    assert.ok(/sync/i.test(out));
  });

  it("is directly callable and respects the platform cap", () => {
    const out = renderEmptyState(result({ isEmpty: true }), "discord");
    assert.ok(out.length <= CHAR_CAP.discord);
  });
});

describe("enforceCap", () => {
  it("leaves short text unchanged", () => {
    assert.strictEqual(enforceCap("hi", 100), "hi");
  });
  it("truncates and appends a marker", () => {
    const out = enforceCap("a".repeat(50), 20);
    assert.ok(out.length <= 20);
    assert.ok(out.endsWith("[truncated]_"));
  });

  it("never exceeds the cap even when the cap is smaller than the marker", () => {
    const out = enforceCap("a".repeat(50), 5);
    assert.ok(out.length <= 5);
  });

  it("does not split a surrogate pair (emoji) mid-truncation", () => {
    // Fill the budget so the cut lands right on an emoji boundary.
    const text = "a".repeat(30) + "😀".repeat(20);
    const out = enforceCap(text, 32);
    assert.ok(out.length <= 32);
    // No unpaired surrogate left dangling before the marker.
    const beforeMarker = out.replace("\n…_[truncated]_", "");
    const lastCode = beforeMarker.charCodeAt(beforeMarker.length - 1);
    assert.ok(!(lastCode >= 0xd800 && lastCode <= 0xdbff), "left a lone high surrogate");
  });
});

describe("relativeTime", () => {
  const now = Date.parse("2026-06-04T12:00:00Z");
  it("formats minutes/hours/days ago", () => {
    assert.strictEqual(relativeTime("2026-06-04T11:30:00Z", now), "30m ago");
    assert.strictEqual(relativeTime("2026-06-04T09:00:00Z", now), "3h ago");
    assert.strictEqual(relativeTime("2026-06-01T12:00:00Z", now), "3d ago");
    assert.strictEqual(relativeTime("2026-06-04T11:59:30Z", now), "just now");
  });
  it("pins the exclusive 60m / 24h thresholds (off-by-one guard)", () => {
    assert.strictEqual(relativeTime("2026-06-04T11:00:00Z", now), "1h ago"); // exactly 60m
    assert.strictEqual(relativeTime("2026-06-04T11:01:00Z", now), "59m ago"); // just under 60m
    assert.strictEqual(relativeTime("2026-06-03T12:00:00Z", now), "1d ago"); // exactly 24h
    assert.strictEqual(relativeTime("2026-06-03T13:00:00Z", now), "23h ago"); // just under 24h
    assert.strictEqual(relativeTime("2026-06-03T11:00:00Z", now), "1d ago"); // 25h still 1d
  });
  it("collapses a future timestamp (clock skew) to 'just now' instead of 'in N hours'", () => {
    assert.strictEqual(relativeTime("2026-06-04T13:00:00Z", now), "just now");
  });
  it("returns null for unparseable input", () => {
    assert.strictEqual(relativeTime("not-a-date", now), null);
  });
});
