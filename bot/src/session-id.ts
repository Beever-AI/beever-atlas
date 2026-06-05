import { createHmac } from "node:crypto";

/**
 * Derive a STABLE, thread-scoped session id for backend conversation memory.
 *
 * Passing a stable `session_id` to `/ask` lets the backend resume the thread's
 * chat history, so follow-up questions remember the prior exchange.
 *
 * SECURITY — the id is keyed on the THREAD, never the channel or the user:
 *  - channel-only would merge unrelated threads into one history;
 *  - user-only would bleed a user's history across channels.
 * Everyone who can see a thread already shares its messages, so thread-scoping
 * crosses no new privacy boundary; the platform's own ACL gates thread access.
 * For DMs the thread id encodes the DM pair, so the session is naturally private.
 * The value is HMAC-hashed (keyed by BOT_SESSION_SECRET) so platform/channel
 * topology never leaks into stored session keys AND the id is unpredictable even
 * to an insider who knows the raw thread id — set BOT_SESSION_SECRET in prod.
 * The integrity of this derivation IS the security boundary — do not key it on
 * anything broader than the thread.
 */
export function deriveSessionId(threadId: string): string {
  const secret = process.env.BOT_SESSION_SECRET || "beever-bot-default-secret";
  const hash = createHmac("sha256", secret).update(`bot-thread:${threadId}`).digest("hex");
  return `botmem_${hash}`;
}
