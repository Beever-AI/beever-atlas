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

/**
 * Extract the channel id, falling back to the whole id.
 * Slack/Teams/Telegram/Mattermost: the second segment. Discord: the THIRD
 * segment (the second is the guild id) — using the second there routes the
 * `/api/channels/{id}/ask` call to the guild and misses the channel's indexed
 * knowledge entirely.
 */
export function extractChannelId(threadId: string): string {
  const parts = threadId.split(":");
  if (isDiscord(parts) && parts.length >= 3) {
    return parts[2];
  }
  return parts.length >= 2 ? parts[1] : threadId;
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
