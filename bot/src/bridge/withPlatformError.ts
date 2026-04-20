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
import { jsonResponse } from "../http-utils.js";

export type BridgeHandler = (
  req: IncomingMessage,
  res: ServerResponse,
) => Promise<void>;

export function withPlatformError(handler: BridgeHandler): BridgeHandler {
  return async (req: IncomingMessage, res: ServerResponse): Promise<void> => {
    try {
      await handler(req, res);
    } catch (err) {
      const { status, code } = classifyPlatformError(err);
      jsonResponse(res, status, { error: String(err), code });
    }
  };
}
