/**
 * Shared HTTP response helpers.
 *
 * Extracted from bot/src/bridge.ts so that bot/src/bridge/* route modules
 * and bot/src/index.ts can all use the same helper without a circular dep.
 */

import type { IncomingMessage, ServerResponse } from "node:http";

/** Write a JSON response with the given status code. */
export function jsonResponse(res: ServerResponse, status: number, data: unknown): void {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(data));
}

/**
 * Render an unknown thrown value as a short, single-line, stack-trace-free
 * string suitable for inclusion in HTTP error responses.
 *
 * `String(err)` on a typical `Error` returns just `"<Name>: <message>"`, but
 * CodeQL `js/stack-trace-exposure` (alerts #10, #11) flags any flow from a
 * caught error to the response body because some hosts attach `.stack` to
 * the default `toString` (or callers later substitute a thrown object whose
 * `toString` interpolates `.stack`). We extract `err.message` explicitly,
 * collapse whitespace, and cap the length so the response never carries
 * line-numbered frame data even if a future call site goes through here.
 */
const _MAX_ERROR_MESSAGE_LEN = 200;

export function safeErrorMessage(err: unknown): string {
  let raw: string;
  if (err instanceof Error) {
    raw = err.message || err.name || "error";
  } else if (typeof err === "string") {
    raw = err;
  } else if (err && typeof err === "object" && "message" in err) {
    const m = (err as { message?: unknown }).message;
    raw = typeof m === "string" ? m : "error";
  } else {
    raw = "error";
  }
  // Collapse all whitespace runs (including newlines from any accidentally
  // multi-line message) into single spaces, then trim and length-cap.
  const flat = raw.replace(/\s+/g, " ").trim();
  if (flat.length <= _MAX_ERROR_MESSAGE_LEN) return flat;
  return `${flat.slice(0, _MAX_ERROR_MESSAGE_LEN)}…`;
}

/**
 * Static prose dictionary for the closed enum returned by
 * `classifyPlatformError`. Use this — NOT `safeErrorMessage` — when
 * building HTTP error responses.
 *
 * CodeQL `js/stack-trace-exposure` (alert #60) treats any data-flow
 * edge from a caught `Error` to a response body as tainted, including
 * `err.message` access — its taint model can't prove `.message`
 * doesn't carry frame data on arbitrary SDK errors. Routing the
 * response through this dictionary breaks the edge entirely: the
 * outgoing string is derived only from the `code` enum, which is a
 * compile-time literal set. Operators retain full debug visibility
 * via `console.error("...", err)` at every catch site — logs are not
 * the response body.
 *
 * Directive: do NOT call `safeErrorMessage(err)` (or `String(err)`,
 * `err.message`, `err.toString()`) at a response sink. Always derive
 * the response prose from `messageForCode(classifyPlatformError(err).code)`.
 */
const _CODE_MESSAGES: Readonly<Record<string, string>> = Object.freeze({
  NOT_FOUND: "resource not found",
  FORBIDDEN: "access denied",
  RATE_LIMITED: "rate limited",
  NOT_SUPPORTED: "operation not supported",
  PLATFORM_ERROR: "upstream platform error",
});

export function messageForCode(code: string): string {
  // Object.prototype.hasOwnProperty.call avoids prototype-pollution
  // bypass — `code` is a closed enum from classifyPlatformError but
  // the helper is exported, so a future caller could pass arbitrary
  // input.
  if (Object.prototype.hasOwnProperty.call(_CODE_MESSAGES, code)) {
    return _CODE_MESSAGES[code];
  }
  return "internal error";
}

/** Maximum HTTP request body the bot will accept (1 MB). */
export const MAX_BODY_SIZE = 1_048_576;

/** Distinguishes oversize-body rejections from other readBody errors. */
export class BodyTooLargeError extends Error {
  constructor() {
    super("Request body too large");
    this.name = "BodyTooLargeError";
  }
}

/**
 * Read the full HTTP request body, rejecting with BodyTooLargeError when
 * cumulative chunk size exceeds MAX_BODY_SIZE.
 *
 * Does NOT call `req.destroy()` on overflow. In HTTP/1.1, IncomingMessage
 * and ServerResponse share the same net.Socket — destroying the request
 * socket here would also tear down the response socket, making any 413/500
 * response written by the caller unreachable (client receives "socket hang
 * up" instead of the response). The caller is responsible for calling
 * `req.destroy()` AFTER writing the response, to terminate the attacker's
 * stream.
 */
export function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    let data = "";
    let size = 0;
    let rejected = false;
    req.on("data", (chunk: Buffer) => {
      if (rejected) return;
      size += chunk.length;
      if (size > MAX_BODY_SIZE) {
        rejected = true;
        reject(new BodyTooLargeError());
        return;
      }
      data += chunk.toString();
    });
    req.on("end", () => {
      if (!rejected) resolve(data);
    });
    req.on("error", (err) => {
      if (!rejected) reject(err);
    });
  });
}
