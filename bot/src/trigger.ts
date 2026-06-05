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
