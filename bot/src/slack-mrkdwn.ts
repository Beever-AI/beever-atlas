/**
 * Slack mrkdwn → clean plain text converter.
 *
 * Handles:
 * - `<url|label>` links → `label`
 * - `<@U123>` user mentions → `@DisplayName` (via userMap)
 * - `<#C123|channel>` channel mentions → `#channel`
 * - `<!here>`, `<!channel>`, `<!everyone>` → `@here`, `@channel`, `@everyone`
 * - `<!subteam^...|@group>` → `@group`
 * - `&gt;` / `&lt;` / `&amp;` HTML entities
 * - `*bold*`, `_italic_`, `~strike~` → plain text (strip markers)
 */

export type UserProfile = { name: string; image: string | null };

export function cleanSlackMrkdwn(
  text: string,
  userMap?: Map<string, UserProfile>,
): string {
  if (!text) return "";

  let cleaned = text;

  // 1. Resolve Slack link/mention markup: <...>
  cleaned = cleaned.replace(/<([^>]+)>/g, (_match, inner: string) => {
    // User mention: <@U123> or <@U123|fallback>
    if (inner.startsWith("@")) {
      const userId = inner.split("|")[0].slice(1);
      const fallbackName = inner.includes("|") ? inner.split("|")[1] : null;
      const resolved = userMap?.get(userId);
      return `@${resolved?.name || fallbackName || userId}`;
    }

    // Channel mention: <#C123|channel-name>
    if (inner.startsWith("#")) {
      const parts = inner.split("|");
      return `#${parts[1] || parts[0].slice(1)}`;
    }

    // Special mentions: <!here>, <!channel>, <!everyone>, <!subteam^...|@group>
    if (inner.startsWith("!")) {
      const keyword = inner.split("|")[0].slice(1);
      const label = inner.includes("|") ? inner.split("|")[1] : null;
      if (label) return label;
      return `@${keyword}`;
    }

    // URL with label: <https://example.com|Click here>
    if (inner.includes("|")) {
      return inner.split("|")[1];
    }

    // Bare URL: <https://example.com>
    return inner;
  });

  // 2. Decode HTML entities that Slack uses.
  //
  // Order matters (CodeQL js/double-escaping, alert #8): decode `&amp;`
  // LAST. Slack escapes user-typed `&` as `&amp;`, so an original input
  // of literally `&lt;` arrives as `&amp;lt;`. If we decode `&amp;` →
  // `&` first, the result `&lt;` then gets decoded to `<`, swallowing
  // the user's literal text. Decoding the angle-brackets first leaves
  // any `&amp;`-escaped sequences intact for the final `&amp;` → `&`
  // pass, so a literal `&lt;` survives as `&lt;` text.
  cleaned = cleaned.replace(/&lt;/g, "<");
  cleaned = cleaned.replace(/&gt;/g, ">");
  cleaned = cleaned.replace(/&amp;/g, "&");

  // 3. Strip bold/italic/strikethrough markers (preserve content)
  //    Use non-greedy match, avoid stripping mid-word underscores (e.g. snake_case)
  cleaned = cleaned.replace(/(?<!\w)\*([^*\n]+)\*(?!\w)/g, "$1");
  cleaned = cleaned.replace(/(?<!\w)_([^_\n]+)_(?!\w)/g, "$1");
  cleaned = cleaned.replace(/(?<!\w)~([^~\n]+)~(?!\w)/g, "$1");

  // 4. Clean up remaining unpaired formatting artifacts:
  //    - Leading "* " or "\n* " (Slack bullet/bold remnants)
  //    - Trailing lone "*" at end of a word/line
  //    - Double "**" from collapsed bold markers
  //    - Leading/trailing unpaired "_" (italic remnants)
  //    - Leading ">" blockquote markers → indent with spaces for readability
  cleaned = cleaned.replace(/^\*\s+/gm, "");
  cleaned = cleaned.replace(/\*{2,}/g, "");
  cleaned = cleaned.replace(/\s\*$/gm, "");
  cleaned = cleaned.replace(/^_(\S)/gm, "$1");
  cleaned = cleaned.replace(/(\S)_$/gm, "$1");
  cleaned = cleaned.replace(/^>/gm, "  ");

  return cleaned;
}
