import { describe, it } from "node:test";
import assert from "node:assert";
import { buildReplyMetric, logReplySent, type ReplyMetric } from "./reply-metrics.js";

const base = {
  surface: "mention" as const,
  platform: "slack",
  route: "qa_agent",
  latencyMs: 1234.6,
  isEmpty: false,
  citationCount: 3,
  followUpCount: 2,
};

describe("buildReplyMetric", () => {
  it("produces the reply_sent record with rounded latency", () => {
    const m = buildReplyMetric(base);
    assert.strictEqual(m.event, "reply_sent");
    assert.strictEqual(m.surface, "mention");
    assert.strictEqual(m.latencyMs, 1235);
    assert.strictEqual(m.citationCount, 3);
    assert.strictEqual(m.followUpCount, 2);
  });

  it("clamps negative latency/counts to zero", () => {
    const m = buildReplyMetric({ ...base, latencyMs: -5, citationCount: -1, followUpCount: -9 });
    assert.strictEqual(m.latencyMs, 0);
    assert.strictEqual(m.citationCount, 0);
    assert.strictEqual(m.followUpCount, 0);
  });

  it("carries NO PII (no thread/author/question/answer fields)", () => {
    const m = buildReplyMetric(base);
    const keys = Object.keys(m).sort();
    assert.deepStrictEqual(keys, [
      "citationCount",
      "event",
      "followUpCount",
      "isEmpty",
      "latencyMs",
      "platform",
      "route",
      "surface",
    ]);
  });
});

describe("logReplySent", () => {
  it("emits the built metric to the injected sink", () => {
    const seen: ReplyMetric[] = [];
    logReplySent(base, (m) => seen.push(m));
    assert.strictEqual(seen.length, 1);
    assert.strictEqual(seen[0].event, "reply_sent");
    assert.strictEqual(seen[0].platform, "slack");
  });
});
