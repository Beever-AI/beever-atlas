/**
 * Extracted classifier for platform SDK errors — kept as its own module so
 * unit tests can import it without pulling in the full bridge HTTP server.
 *
 * WS-M8 regression: the `&&` branch in the auth block binds tighter than
 * the surrounding `||`s. Do NOT rewrite without adding explicit parentheses.
 */

export function classifyPlatformError(err: unknown): { status: number; code: string } {
  const msg = String(err).toLowerCase();
  const errData = (err as any)?.data?.error || "";
  const errCode = (err as any)?.code || "";

  // Not-found errors
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

  // Rate limiting
  if (
    errData === "ratelimited" ||
    msg.includes("rate limit") ||
    msg.includes(": 429")
  ) {
    return { status: 429, code: "RATE_LIMITED" };
  }

  // Not supported
  if (
    errData === "not_supported" ||
    (err as any)?.code === "NOT_SUPPORTED" ||
    msg.includes("not supported")
  ) {
    return { status: 501, code: "NOT_SUPPORTED" };
  }

  return { status: 502, code: "PLATFORM_ERROR" };
}
