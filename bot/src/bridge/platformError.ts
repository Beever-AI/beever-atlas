/**
 * Canonical platform error classifier.
 *
 * Single source of truth for translating Chat SDK / HTTP errors into a
 * normalised { status, code } pair.  Every import site MUST resolve to this
 * module — bot/src/bridge-classifier.ts has been deleted.
 *
 * WS-M8 regression: the `&&` branch in the auth block binds tighter than the
 * surrounding `||`s.  Do NOT rewrite without adding explicit parentheses.
 */

/**
 * Shape of a Slack/Discord/etc. platform SDK error.  The SDK wraps the raw
 * HTTP payload under `data` and exposes a semantic `code` at the top level.
 * All fields are optional because upstream SDKs disagree on what they set.
 */
export interface PlatformErrorShape {
  code?: string;
  data?: { error?: string };
}

/** Classify a platform SDK error into an appropriate HTTP status code. */
export function classifyPlatformError(err: unknown): { status: number; code: string } {
  const msg = String(err).toLowerCase();
  const shape = (err ?? {}) as PlatformErrorShape;
  const errData = shape.data?.error ?? "";
  const errCode = shape.code ?? "";

  // Not-found errors (Slack: channel_not_found, file_not_found; Discord: 404; generic)
  if (
    errData === "channel_not_found" ||
    errData === "file_not_found" ||
    errData === "thread_not_found" ||
    errData === "not_found" ||
    msg.includes("not found") ||
    msg.includes("not_found") ||
    msg.includes(": 404")
  ) {
    return { status: 404, code: "NOT_FOUND" };
  }

  // Permission / auth errors
  if (
    errData === "not_authed" ||
    errData === "invalid_auth" ||
    errData === "token_revoked" ||
    errData === "missing_scope" ||
    errData === "not_allowed_token_type" ||
    (errCode === "slack_webapi_platform_error" && errData === "not_in_channel") ||
    msg.includes("forbidden") ||
    msg.includes(": 403")
  ) {
    return { status: 403, code: "FORBIDDEN" };
  }

  // Rate limiting (shouldn't normally reach here, but just in case)
  if (
    errData === "ratelimited" ||
    msg.includes("rate limit") ||
    msg.includes(": 429")
  ) {
    return { status: 429, code: "RATE_LIMITED" };
  }

  // Not supported (Teams/Telegram stubs)
  if (
    errData === "not_supported" ||
    (err as PlatformErrorShape)?.code === "NOT_SUPPORTED" ||
    msg.includes("not supported")
  ) {
    return { status: 501, code: "NOT_SUPPORTED" };
  }

  // Default: upstream platform error — use 502 (bad gateway)
  return { status: 502, code: "PLATFORM_ERROR" };
}
