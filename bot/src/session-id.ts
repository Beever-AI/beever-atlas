import { createHmac } from "node:crypto";

/**
 * Idle window for loose top-level @mentions. Back-to-back mentions from the same
 * (user, channel) within this window share one session (continuity); after this
 * much silence, the next mention starts a fresh session.
 */
const IDLE_WINDOW_MS = 30 * 60 * 1000;

/**
 * Derive a STABLE session id for backend conversation memory.
 *
 * Passing a stable `session_id` to `/ask` lets the backend resume a prior
 * exchange, so follow-up questions remember context.
 *
 * Two keying modes, chosen by `isThreaded`:
 *  - THREADED (`isThreaded === true`) — a reply inside an actual thread. Key on
 *    the THREAD (`bot-thread:${threadId}`) for precise continuity: every message
 *    in that thread resumes exactly that thread's history.
 *  - LOOSE TOP-LEVEL (`isThreaded === false`) — a bare top-level @mention that is
 *    NOT inside a thread. There is no thread to scope to, so we key on
 *    (user, channel) within a 30-minute IDLE WINDOW
 *    (`bot-topmention:${userId}:${channelId}:${bucket}`). This gives continuity
 *    for back-to-back mentions from the same user in the same channel, then a
 *    fresh session once they've been idle past the window.
 *
 * SECURITY — neither mode keys on anything broader than is safe:
 *  - thread-only would never be too broad (everyone who sees a thread shares it);
 *  - the top-level mode is (user, channel)-scoped, so a user's loose-mention
 *    history never bleeds across channels, and the time-bucket prevents an
 *    indefinitely-shared key. channel-only would merge unrelated users; we never
 *    do that. For DMs the thread id encodes the DM pair, so a threaded DM session
 *    is naturally private.
 * The value is HMAC-hashed (keyed by BOT_SESSION_SECRET) so platform/channel
 * topology never leaks into stored session keys AND the id is unpredictable even
 * to an insider who knows the raw thread/user/channel ids — set BOT_SESSION_SECRET
 * in prod. The integrity of this derivation IS the security boundary — do not key
 * it on anything broader than (thread) or (user, channel, idle-bucket).
 *
 * `userId` is falsy-safe: callers pass "unknown" when the author id is absent.
 */
export function deriveSessionId(
  threadId: string,
  userId: string,
  channelId: string,
  isThreaded: boolean,
): string {
  const secret = process.env.BOT_SESSION_SECRET || "beever-bot-default-secret";
  let material: string;
  if (isThreaded) {
    material = `bot-thread:${threadId}`;
  } else {
    const bucket = Math.floor(Date.now() / IDLE_WINDOW_MS) * IDLE_WINDOW_MS;
    material = `bot-topmention:${userId}:${channelId}:${bucket}`;
  }
  const hash = createHmac("sha256", secret).update(material).digest("hex");
  return `botmem_${hash}`;
}
