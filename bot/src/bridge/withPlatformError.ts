/**
 * Higher-order wrapper that applies classifyPlatformError to any unhandled
 * error thrown by a route handler and emits a normalised { error, code }
 * JSON envelope.
 *
 * Usage:
 *   const handler = withPlatformError(async (req, res) => { ... });
 */

import type { IncomingMessage, ServerResponse } from "node:http";
import { classifyPlatformError } from "./platformError.js";
import { jsonResponse, messageForCode } from "../http-utils.js";

export type BridgeHandler = (
  req: IncomingMessage,
  res: ServerResponse,
) => Promise<void>;

export function withPlatformError(handler: BridgeHandler): BridgeHandler {
  return async (req: IncomingMessage, res: ServerResponse): Promise<void> => {
    try {
      await handler(req, res);
    } catch (err) {
      // CodeQL js/stack-trace-exposure (alert #60): the response prose
      // is derived ONLY from the closed `code` enum, never from `err`.
      // The full error is still available to operators via console.error.
      const { status, code } = classifyPlatformError(err);
      console.error("Bridge: handler error:", err);
      jsonResponse(res, status, { error: messageForCode(code), code });
    }
  };
}
