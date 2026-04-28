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
