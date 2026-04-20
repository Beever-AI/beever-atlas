/**
 * Shared HTTP response helpers.
 *
 * Extracted from bot/src/bridge.ts so that bot/src/bridge/* route modules
 * and bot/src/index.ts can all use the same helper without a circular dep.
 */

import type { ServerResponse } from "node:http";

/** Write a JSON response with the given status code. */
export function jsonResponse(res: ServerResponse, status: number, data: unknown): void {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(data));
}
