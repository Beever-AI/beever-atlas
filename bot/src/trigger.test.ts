import { describe, it } from "node:test";
import assert from "node:assert";
import {
  classifyIntent,
  decideShouldRespond,
  decideSubscribedAction,
  decideSubscribedThreadActionWithLookup,
  isBroadcast,
  type RespondInput,
} from "./trigger.js";

const T = 2; // quiet threshold used across cases

describe("decideSubscribedAction", () => {
  it("skips the bot's own messages", () => {
    assert.strictEqual(
      decideSubscribedAction({ isMe: true, isMention: true, quietThreshold: T }),
      "skip",
    );
  });

  it("skips other bots", () => {
    assert.strictEqual(
      decideSubscribedAction({ isBot: true, quietThreshold: T }),
      "skip",
    );
  });

  it("treats isBot='unknown' as a human (does not skip)", () => {
    assert.strictEqual(
      decideSubscribedAction({ isBot: "unknown", humanCount: 1, quietThreshold: T }),
      "answer",
    );
  });

  it("always answers an explicit mention, even in a busy thread", () => {
    assert.strictEqual(
      decideSubscribedAction({ isMention: true, humanCount: 9, quietThreshold: T }),
      "answer",
    );
  });

  it("answers non-mention follow-ups while effectively 1:1", () => {
    assert.strictEqual(
      decideSubscribedAction({ isMention: false, humanCount: 1, quietThreshold: T }),
      "answer",
    );
  });

  it("withdraws (unsubscribe) from a multi-human, non-mention thread", () => {
    assert.strictEqual(
      decideSubscribedAction({ isMention: false, humanCount: 2, quietThreshold: T }),
      "unsubscribe",
    );
    assert.strictEqual(
      decideSubscribedAction({ isMention: false, humanCount: 5, quietThreshold: T }),
      "unsubscribe",
    );
  });

  it("answers when participant count is unknown (never goes silent on uncertainty)", () => {
    assert.strictEqual(
      decideSubscribedAction({ isMention: false, quietThreshold: T }),
      "answer",
    );
  });

  it("prioritizes self/bot skip over mention", () => {
    assert.strictEqual(
      decideSubscribedAction({ isMe: true, isBot: true, isMention: true, quietThreshold: T }),
      "skip",
    );
  });
});

describe("decideSubscribedThreadActionWithLookup", () => {
  it("answers a mention WITHOUT calling the participant lookup", async () => {
    let calls = 0;
    const action = await decideSubscribedThreadActionWithLookup(
      { isMention: true, quietThreshold: T },
      async () => { calls += 1; return 9; },
    );
    assert.strictEqual(action, "answer");
    assert.strictEqual(calls, 0);
  });

  it("skips self/bot without calling the lookup", async () => {
    let calls = 0;
    const a = await decideSubscribedThreadActionWithLookup(
      { isMe: true, quietThreshold: T },
      async () => { calls += 1; return 0; },
    );
    assert.strictEqual(a, "skip");
    assert.strictEqual(calls, 0);
  });

  it("unsubscribes a multi-human non-mention thread (one lookup call)", async () => {
    let calls = 0;
    const action = await decideSubscribedThreadActionWithLookup(
      { isMention: false, quietThreshold: T },
      async () => { calls += 1; return 2; },
    );
    assert.strictEqual(action, "unsubscribe");
    assert.strictEqual(calls, 1);
  });

  it("answers when the lookup returns undefined (never silent on unknown)", async () => {
    const action = await decideSubscribedThreadActionWithLookup(
      { isMention: false, quietThreshold: T },
      async () => undefined,
    );
    assert.strictEqual(action, "answer");
  });
});

describe("isBroadcast", () => {
  it("detects @channel / @here / @everyone (normalized + Slack-raw)", () => {
    for (const t of ["@channel deploy done", "heads up @here", "@everyone please read", "<!channel> alert", "<!here>", "<!everyone|@everyone> ping"]) {
      assert.strictEqual(isBroadcast(t), true, t);
    }
  });
  it("ignores normal text and the bot mention", () => {
    for (const t of ["what is our deploy process?", "@beever summarize this", "the #channel topic"]) {
      assert.strictEqual(isBroadcast(t), false, t);
    }
  });
});

describe("classifyIntent", () => {
  it("flags questions and requests", () => {
    for (const t of ["what is our stack?", "who won?", "how does this work", "is the deploy done", "summarize the thread", "summary of the thread", "tell me about X", "can you find the decision", "remind me what we said"]) {
      assert.strictEqual(classifyIntent(t), "question", t);
    }
  });
  it("flags pleasantries", () => {
    for (const t of ["thanks", "thank you!", "ok", "nice", "good job", "👍", "got it", "lol"]) {
      assert.strictEqual(classifyIntent(t), "pleasantry", t);
    }
  });
  it("treats statements as statements (not questions)", () => {
    for (const t of ["the meeting is at 3pm", "deploy is done", "standup moved to 4", "the data finished syncing"]) {
      assert.strictEqual(classifyIntent(t), "statement", t);
    }
  });
  it("treats empty as empty", () => {
    assert.strictEqual(classifyIntent("   "), "empty");
  });
});

describe("decideShouldRespond", () => {
  const d = (o: Partial<RespondInput>): string =>
    decideShouldRespond({ text: "", broadcast: false, isMention: false, surface: "mention", ...o });

  it("answers a direct question", () => {
    assert.strictEqual(d({ text: "what is our stack?", isMention: true }), "respond");
  });
  it("nudges on a bare mention", () => {
    assert.strictEqual(d({ text: "", isMention: true, surface: "mention" }), "prompt");
  });
  it("stays quiet on a pleasantry mention", () => {
    assert.strictEqual(d({ text: "thanks!", isMention: true }), "skip");
  });
  it("stays quiet on a broadcast announcement even when tagged", () => {
    assert.strictEqual(
      d({ text: "deploy is done", broadcast: true, isMention: true }),
      "skip",
    );
  });
  it("still answers a real question inside a broadcast", () => {
    assert.strictEqual(
      d({ text: "what's our deploy process?", broadcast: true, isMention: true }),
      "respond",
    );
  });
  it("answers a statement that directly mentions the bot", () => {
    assert.strictEqual(d({ text: "the Q3 decision", isMention: true }), "respond");
  });
  it("ignores a non-mention statement in a joined thread", () => {
    assert.strictEqual(d({ text: "the meeting moved to 3pm", surface: "follow-up" }), "skip");
  });
  it("answers a non-mention question in a joined thread", () => {
    assert.strictEqual(d({ text: "what about Friday?", surface: "follow-up" }), "respond");
  });
  it("answers a request-verb message in a joined thread", () => {
    assert.strictEqual(d({ text: "find the Q3 decision", surface: "follow-up" }), "respond");
  });
  it("skips an empty follow-up (no nudge spam in threads)", () => {
    assert.strictEqual(d({ text: "", surface: "follow-up" }), "skip");
  });
  it("answers statements in a DM but skips DM pleasantries", () => {
    assert.strictEqual(d({ text: "the deploy finished", surface: "dm", isMention: true }), "respond");
    assert.strictEqual(d({ text: "thanks!", surface: "dm", isMention: true }), "skip");
  });
});
