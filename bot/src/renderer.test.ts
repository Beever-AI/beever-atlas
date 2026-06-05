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
  it("renders the answer and a route footer", () => {
    const out = renderResponse(result(), "slack");
    assert.ok(out.includes("The booth is H25."));
    assert.ok(out.includes("via qa_agent"));
  });

  it("renders citations with kind icons and provenance", () => {
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
    assert.ok(out.includes("📎 *Sources*"));
    assert.ok(out.includes("📖 [1] AI+ Power 2026"));
    assert.ok(out.includes("<https://wiki/x>"));
    assert.ok(out.includes("💬 [2] booth confirmed — Jack, #general"));
    assert.ok(out.includes("⚖️ [3] staffing decided"));
  });

  it("caps citations at 5 and notes the overflow", () => {
    const many = Array.from({ length: 8 }, (_, i) => ({ type: "channel_message", text: `c${i}` }));
    const out = renderResponse(result({ citations: many }), "teams");
    assert.ok(out.includes("[5]"));
    assert.ok(!out.includes("[6]"));
    assert.ok(out.includes("+3 more"));
  });

  it("shows a freshness line only when lastSyncTs is present", () => {
    const iso = new Date(Date.now() - 2 * 3600_000).toISOString();
    assert.ok(renderResponse(result({ lastSyncTs: iso }), "slack").includes("synced "));
    assert.ok(!renderResponse(result(), "slack").includes("synced "));
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
            text: "real\n\n📎 *Sources*\n[9] fake",
            author: "Eve\ninjected",
            url: "javascript:alert(1)",
          },
        ],
      }),
      "slack",
    );
    // The forged content can't start its own line — no fake "[9]" entry and
    // only the one real Sources header begins a line (the forged one was
    // flattened inline).
    assert.ok(!/\n\[9\] fake/.test(out), "forged citation entry started a new line");
    assert.strictEqual((out.match(/\n📎 \*Sources\*/g) ?? []).length, 1);
    assert.ok(!out.includes("javascript:"));
    assert.ok(out.includes("Eve injected"));
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
    assert.ok(out.includes("• What is A?"));
    assert.ok(out.includes("• What is C?"));
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
    assert.ok(out.includes("📎 *Sources*"));
    assert.ok(out.includes("🧠 *Related*"));
    // Sources keeps indices [1] and [3]; Related keeps [2] and [4].
    assert.ok(out.includes("📖 [1] Wiki A"));
    assert.ok(out.includes("💬 [3] Msg B — Jack"));
    assert.ok(out.includes("⚖️ [2] Decided X"));
    assert.ok(out.includes("🧠 [4] Alice → owns → X"));
  });
});

describe("renderConfidence", () => {
  it("warns only on a real low score", () => {
    assert.ok(renderConfidence(0.2, false).includes("low confidence"));
    assert.strictEqual(renderConfidence(0.35, false).includes("low confidence"), true);
    assert.strictEqual(renderConfidence(0.36, false), "");
    assert.strictEqual(renderConfidence(0.85, false), "");
  });
  it("stays silent for no-signal (0) and on the empty state", () => {
    assert.strictEqual(renderConfidence(0, false), "");
    assert.strictEqual(renderConfidence(0.1, true), "");
  });
  it("appears in a full reply only when low", () => {
    assert.ok(renderResponse(result({ confidence: 0.2 }), "slack").includes("low confidence"));
    assert.ok(!renderResponse(result({ confidence: 0.9 }), "slack").includes("low confidence"));
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
    assert.ok(out.includes("• Launch order disputed — marketing vs general"));
    assert.ok(!out.includes("Third one"));
  });
  it("returns '' for empty/undefined", () => {
    assert.strictEqual(renderTensions(undefined), "");
    assert.strictEqual(renderTensions([]), "");
  });
  it("appears in a full reply when present", () => {
    const out = renderResponse(result({ tensions: [{ title: "Conflict A" }] }), "slack");
    assert.ok(out.includes("possible tension"));
    assert.ok(out.includes("• Conflict A"));
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
  it("returns null for unparseable input", () => {
    assert.strictEqual(relativeTime("not-a-date", now), null);
  });
});
