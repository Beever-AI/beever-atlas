/**
 * Strip a bot @mention from inbound text, per platform.
 *
 * Previously only Slack's `<@U123>` form was removed, so Discord/Teams/Telegram/
 * Mattermost mention tokens leaked into the question and degraded the answer.
 * The bracketed forms (Slack/Discord/Teams) are always safe to strip; the bare
 * `@handle` form (Mattermost/Telegram) is only stripped at the start of the
 * message and only for those platforms, to avoid eating a legitimate "@channel"
 * inside a question.
 */

// Slack `<@U123>` / `<@U123|name>` and Discord `<@123>` / `<@!123>` / `<@&123>`.
const BRACKET_USER = /<@[!&]?[A-Z0-9]+(\|[^>]+)?>/gi;
// Teams HTML mention `<at>Display Name</at>`.
const TEAMS_AT = /<at>[^<]*<\/at>/gi;
// Bare `@handle` at the very start of the message (Mattermost / Telegram).
const LEADING_HANDLE = /^\s*@[\w.-]+\s*/;

export function stripMention(text: string, platform?: string): string {
  let out = text.replace(BRACKET_USER, " ").replace(TEAMS_AT, " ");

  const p = (platform || "").toLowerCase();
  if (p === "mattermost" || p === "telegram") {
    out = out.replace(LEADING_HANDLE, " ");
  }

  return out.replace(/\s+/g, " ").trim();
}
