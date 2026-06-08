/**
 * Helpers for parsing Chat SDK thread ids.
 *
 * Most platforms encode `"<platform>:<channelId>:<thread>"`
 * (e.g. `"slack:C123:1700000000.000100"`). **Discord is the exception**: its
 * adapter encodes an extra leading guild segment —
 * `"discord:<guildId>:<channelId>[:<thread>]"` — so for Discord the channel is
 * the THIRD token and the thread is the FOURTH, each shifted one past every
 * other platform. The helpers special-case Discord; all other platforms keep
 * the `<platform>:<channelId>:<thread>` layout unchanged.
 *
 * The platform prefix is the only place the post-time platform is recoverable,
 * since the SDK `Thread`/`Message` objects passed to handlers carry no platform
 * field.
 */

/** True when a thread id uses Discord's extra-leading-guild-segment layout. */
function isDiscord(parts: string[]): boolean {
  return parts[0]?.toLowerCase() === "discord";
}

/** A canonical Mattermost id is 26 lowercase base32 chars (a–z, 0–9). */
const MATTERMOST_ID_RE = /^[a-z0-9]{26}$/;

/**
 * Recover the RAW Mattermost channel id from a thread-id segment.
 *
 * The SDK base64-encodes the Mattermost channel id inside the thread id
 * (`mattermost:<base64-channel>:<thread>`), but ingestion/sync — and therefore
 * the stored facts, wiki, and the `/api/channels/{id}/...` routes — key on the
 * RAW Mattermost channel id. Passing the base64 segment to `/ask` queries a
 * non-existent channel and returns "nothing indexed" even for a fully-synced
 * channel (observed live). Decode it back. Guarded: only accept a decode that
 * yields a canonical 26-char Mattermost id, so a segment that is already raw
 * (or any non-base64 value) is returned unchanged.
 */
function decodeMattermostChannel(segment: string): string {
  try {
    const decoded = Buffer.from(segment, "base64").toString("utf8");
    if (MATTERMOST_ID_RE.test(decoded)) return decoded;
  } catch {
    /* not base64 — fall through to the raw segment */
  }
  return segment;
}

/**
 * Extract the channel id, falling back to the whole id.
 * Slack/Teams/Telegram: the second segment. Discord: the THIRD segment (the
 * second is the guild id) — using the second there routes the
 * `/api/channels/{id}/ask` call to the guild and misses the channel's indexed
 * knowledge entirely. Mattermost: the second segment, base64-DECODED to the raw
 * channel id that ingestion stored facts under (see decodeMattermostChannel).
 */
export function extractChannelId(threadId: string): string {
  const parts = threadId.split(":");
  if (isDiscord(parts) && parts.length >= 3) {
    return parts[2];
  }
  if (parts.length < 2) return threadId;
  if (parts[0]?.toLowerCase() === "mattermost") {
    return decodeMattermostChannel(parts[1]);
  }
  return parts[1];
}

/**
 * Extract the thread segment, falling back to the whole id when there is no
 * thread token. A thread id can itself contain colons (e.g. a Slack message ts
 * joined with a parent), so we keep the full remainder rather than just one
 * token. For Discord the thread starts one token later (after the guild).
 */
export function extractThreadId(threadId: string): string {
  const parts = threadId.split(":");
  if (isDiscord(parts)) {
    return parts.length >= 4 ? parts.slice(3).join(":") : threadId;
  }
  return parts.length >= 3 ? parts.slice(2).join(":") : threadId;
}

/**
 * Extract the platform (first segment), lower-cased. Returns `"unknown"` for
 * malformed ids so the renderer degrades to a safe generic format rather than
 * throwing.
 */
export function extractPlatform(threadId: string): string {
  const parts = threadId.split(":");
  return parts.length >= 2 && parts[0] ? parts[0].toLowerCase() : "unknown";
}

/**
 * True when this thread id carries a real, NON-EMPTY thread segment
 * (`<platform>:<channelId>:<thread>`) — i.e. it identifies a stable thread root
 * we can key a session on. The SDK always encodes one (Slack uses
 * `event.thread_ts || event.ts`, so even a root @mention has the root ts here),
 * so this is true in practice; it returns false only for a degenerate/malformed
 * id with no thread segment, where the caller should fall back to the loose
 * idle-window key instead of keying on an unstable value.
 */
export function hasThreadRoot(threadId: string): boolean {
  const parts = threadId.split(":");
  if (isDiscord(parts)) {
    return parts.length >= 4 && parts.slice(3).join(":").trim().length > 0;
  }
  return parts.length >= 3 && parts.slice(2).join(":").trim().length > 0;
}
