/**
 * Trigger gate for subscribed-thread messages.
 *
 * Once the bot is @mentioned in a thread it calls `thread.subscribe()`, after
 * which the SDK routes EVERY message in that thread to `onSubscribedMessage` —
 * including the bot's own replies and messages that don't mention it. Without a
 * gate the bot answers everything, which is the "why does it reply so many
 * times" bug. This pure decision function encodes the intended behavior so it
 * can be unit-tested without a live SDK.
 *
 * Rules (in order):
 *   1. Never answer the bot's own messages (`isMe`).
 *   2. Never answer other bots (`isBot === true`). `"unknown"` is treated as a
 *      human (we don't suppress on uncertainty).
 *   3. An explicit @mention always gets an answer, even in a busy channel.
 *   4. Otherwise, if the thread has become a multi-human conversation
 *      (`humanCount >= quietThreshold`), withdraw (`unsubscribe`) and stay quiet
 *      so humans can talk without the bot interjecting.
 *   5. Otherwise the thread is still effectively 1:1 with the bot — answer.
 */

export type SubscribedAction = "answer" | "skip" | "unsubscribe";

export interface SubscribedDecisionInput {
  /** Message author is the bot itself. */
  isMe?: boolean;
  /** Message author is a bot. `"unknown"` is NOT treated as a bot. */
  isBot?: boolean | "unknown";
  /** Message explicitly @mentions the bot. */
  isMention?: boolean;
  /**
   * Number of human participants in the thread (bot excluded). `undefined`
   * means "couldn't determine" — we err toward answering rather than going
   * silent, so a transient lookup failure never makes the bot mute.
   */
  humanCount?: number;
  /** Human count at/above which the bot withdraws from a non-mention thread. */
  quietThreshold: number;
}

export function decideSubscribedAction(input: SubscribedDecisionInput): SubscribedAction {
  if (input.isMe === true) return "skip";
  if (input.isBot === true) return "skip";
  if (input.isMention === true) return "answer";
  if (typeof input.humanCount === "number" && input.humanCount >= input.quietThreshold) {
    return "unsubscribe";
  }
  return "answer";
}

export interface SubscribedThreadInput {
  isMe?: boolean;
  isBot?: boolean | "unknown";
  isMention?: boolean;
  quietThreshold: number;
}

/**
 * Orchestrate the subscribed-thread decision, fetching the human count ONLY
 * when it can change the outcome (not self/bot, not a mention). `getHumanCount`
 * is injected — so this stays unit-testable without a live SDK — and may return
 * `undefined` for "unknown", in which case we never go silent.
 */
export async function decideSubscribedThreadActionWithLookup(
  input: SubscribedThreadInput,
  getHumanCount: () => Promise<number | undefined>,
): Promise<SubscribedAction> {
  const { isMe, isBot, isMention, quietThreshold } = input;
  if (isMe === true || isBot === true || isMention === true) {
    return decideSubscribedAction({ isMe, isBot, isMention, quietThreshold });
  }
  const humanCount = await getHumanCount();
  return decideSubscribedAction({ isMe, isBot, isMention, humanCount, quietThreshold });
}

// ── Addressing & intent gate ────────────────────────────────────────────────
//
// "Should I stay subscribed?" (above) is separate from "should I SPEAK?". A good
// assistant answers questions it is asked — it does not react to every message
// it is copied on. Broadcast announcements (@channel/@here/@everyone) and
// passive chatter/pleasantries must NOT trigger a knowledge answer, even when
// the bot is tagged or already in the thread. This pure, testable gate encodes
// that, so the bot stays quiet on announcements and only speaks when actually
// being asked something.

export type RespondDecision = "respond" | "skip" | "prompt";
export type MessageIntent = "empty" | "pleasantry" | "question" | "statement";

// @channel / @here / @everyone — normalized OR Slack-raw (<!channel>,
// <!here>, <!everyone|label>). A broadcast addresses the room, not the bot.
const BROADCAST_RE = /(^|\s)@(channel|here|everyone)\b|<!(channel|here|everyone)(\|[^>]*)?>/i;

export function isBroadcast(rawText: string): boolean {
  return BROADCAST_RE.test(rawText || "");
}

// Short acknowledgements / pleasantries that deserve no answer.
const PLEASANTRY_RE =
  /^(thanks?|thank you|thx|ty|cheers|ok(ay)?|k|cool|nice|great|awesome|perfect|good (job|work|one|stuff)|well done|lol+|haha+|yes|yep|yeah|no|nope|nvm|never ?mind|got it|sounds good|will do|👍|🙏|🎉|❤️|👏|✅)[\s!.,]*$/iu;

// Question/request signals, anchored to the START so a statement that merely
// contains "is"/"do" (e.g. "the meeting is at 3pm") is not misread as a question.
const QUESTION_START_RE =
  /^(what'?s?|whats|who'?s?|whom|whose|which|when|where|why|how|is|are|am|was|were|do|does|did|can|could|would|should|will|won'?t|shall|may|might|has|have|had|any|anyone|anybody)\b/i;
const REQUEST_START_RE =
  /^(tell|explain|summar\w*|find|search|show|list|give|describe|compare|recap|remind|help|define|walk me|catch me up|fill me in|let me know|please|pls|can you|could you|i want|i need|i'?d like|need|looking for|how about|what about)\b/i;

export function classifyIntent(text: string): MessageIntent {
  const t = (text || "").trim();
  if (!t) return "empty";
  if (PLEASANTRY_RE.test(t)) return "pleasantry";
  if (t.includes("?")) return "question";
  if (QUESTION_START_RE.test(t) || REQUEST_START_RE.test(t)) return "question";
  return "statement";
}

export interface RespondInput {
  /** Message text AFTER the bot mention is stripped. */
  text: string;
  /** Original message carried a broadcast token (@channel/@here/@everyone). */
  broadcast: boolean;
  /** Message explicitly @mentions the bot. */
  isMention: boolean;
  /** Where the message arrived. */
  surface: "mention" | "follow-up" | "dm";
}

/**
 * Decide whether to answer, stay silent, or nudge for a question.
 *
 *  - empty (bare @mention / empty DM) → "prompt"; an empty thread follow-up → "skip".
 *  - pleasantry ("thanks", "👍") → "skip".
 *  - broadcast announcement that isn't itself a question → "skip" (it's an FYI to
 *    the room, not a request to the bot — even if the bot is tagged).
 *  - question/request → "respond".
 *  - plain statement → "respond" only when addressed directly (explicit mention
 *    or DM); a non-mention statement in a joined thread → "skip".
 */
export function decideShouldRespond(input: RespondInput): RespondDecision {
  const intent = classifyIntent(input.text);
  if (intent === "empty") return input.surface === "follow-up" ? "skip" : "prompt";
  if (intent === "pleasantry") return "skip";
  if (input.broadcast && intent !== "question") return "skip";
  if (intent === "question") return "respond";
  // plain statement, not a broadcast:
  if (input.isMention || input.surface === "dm") return "respond";
  return "skip";
}
