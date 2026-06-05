import { createHash } from "node:crypto";

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
 * The value is hashed so platform/channel topology never leaks into stored
 * session keys, and to give a fixed-width opaque id. The integrity of this
 * derivation IS the security boundary — do not key it on anything broader.
 */
export function deriveSessionId(threadId: string): string {
  const hash = createHash("sha256").update(`bot-thread:${threadId}`).digest("hex");
  return `botmem_${hash}`;
}
