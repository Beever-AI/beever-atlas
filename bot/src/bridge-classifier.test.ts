/**
 * WS-M8 regression: operator-precedence in `classifyPlatformError`.
 *
 * The tricky line is:
 *   errCode === "slack_webapi_platform_error" && errData === "not_in_channel"
 * which sits inside an outer `||` chain. JS evaluates `&&` before `||`, so the
 * whole disjunction is equivalent to:
 *   ... || (errCode === "..." && errData === "not_in_channel") || ...
 * This table-driven test locks in the expected classification for every
 * (errCode, errData, msg) shape we care about, so a future edit that drops a
 * pair of parens or flips an operator will fail loudly.
 */

import { describe, it } from "node:test";
import assert from "node:assert";

// The classifier is not exported — re-implement a pointer via dynamic import of
// the compiled module would require a build step. Instead, use a thin harness
// that calls the exported request handlers to surface the classification via
// the HTTP response code… but since those have many deps, we inline the
// fixture expectations against a local copy of the classifier's contract.
//
// To keep the test hermetic and deps-free, we export the classifier from a
// tiny shim under bot/src for direct import. See bot/src/bridge-classifier.ts.
import { classifyPlatformError } from "./bridge/platformError.js";

interface Case {
  name: string;
  err: unknown;
  expectStatus: number;
  expectCode: string;
}

const cases: Case[] = [
  // Not-found family
  { name: "slack channel_not_found", err: { data: { error: "channel_not_found" } }, expectStatus: 404, expectCode: "NOT_FOUND" },
  { name: "slack file_not_found", err: { data: { error: "file_not_found" } }, expectStatus: 404, expectCode: "NOT_FOUND" },
  { name: "slack thread_not_found", err: { data: { error: "thread_not_found" } }, expectStatus: 404, expectCode: "NOT_FOUND" },
  { name: "generic not_found data", err: { data: { error: "not_found" } }, expectStatus: 404, expectCode: "NOT_FOUND" },
  { name: "discord 404 msg", err: new Error("discord request failed: 404 not-found"), expectStatus: 404, expectCode: "NOT_FOUND" },

  // Auth family
  { name: "not_authed", err: { data: { error: "not_authed" } }, expectStatus: 403, expectCode: "FORBIDDEN" },
  { name: "invalid_auth", err: { data: { error: "invalid_auth" } }, expectStatus: 403, expectCode: "FORBIDDEN" },
  { name: "token_revoked", err: { data: { error: "token_revoked" } }, expectStatus: 403, expectCode: "FORBIDDEN" },
  { name: "missing_scope", err: { data: { error: "missing_scope" } }, expectStatus: 403, expectCode: "FORBIDDEN" },
  { name: "not_allowed_token_type", err: { data: { error: "not_allowed_token_type" } }, expectStatus: 403, expectCode: "FORBIDDEN" },
  {
    name: "slack_webapi_platform_error + not_in_channel (precedence lock)",
    err: { code: "slack_webapi_platform_error", data: { error: "not_in_channel" } },
    expectStatus: 403,
    expectCode: "FORBIDDEN",
  },
  {
    name: "slack_webapi_platform_error but NOT not_in_channel → falls through",
    err: { code: "slack_webapi_platform_error", data: { error: "something_else" } },
    expectStatus: 502,
    expectCode: "PLATFORM_ERROR",
  },
  {
    name: "not_in_channel without slack_webapi_platform_error code → falls through",
    err: { data: { error: "not_in_channel" } },
    expectStatus: 502,
    expectCode: "PLATFORM_ERROR",
  },
  { name: "discord 403 msg", err: new Error("request failed: 403 forbidden-ish"), expectStatus: 403, expectCode: "FORBIDDEN" },

  // Rate-limited
  { name: "slack ratelimited", err: { data: { error: "ratelimited" } }, expectStatus: 429, expectCode: "RATE_LIMITED" },
  { name: "discord 429 msg", err: new Error("request failed: 429 Too Many Requests"), expectStatus: 429, expectCode: "RATE_LIMITED" },
  { name: "rate limit string", err: new Error("hit rate limit"), expectStatus: 429, expectCode: "RATE_LIMITED" },

  // Not supported
  { name: "not_supported data", err: { data: { error: "not_supported" } }, expectStatus: 501, expectCode: "NOT_SUPPORTED" },
  { name: "NOT_SUPPORTED code", err: { code: "NOT_SUPPORTED" }, expectStatus: 501, expectCode: "NOT_SUPPORTED" },
  { name: "not supported msg", err: new Error("feature is not supported"), expectStatus: 501, expectCode: "NOT_SUPPORTED" },

  // Default fallthrough
  { name: "unknown error", err: new Error("kaboom"), expectStatus: 502, expectCode: "PLATFORM_ERROR" },
  { name: "empty object", err: {}, expectStatus: 502, expectCode: "PLATFORM_ERROR" },
];

describe("classifyPlatformError (WS-M8 precedence regression)", () => {
  for (const c of cases) {
    it(c.name, () => {
      const out = classifyPlatformError(c.err);
      assert.strictEqual(out.status, c.expectStatus, `status mismatch for ${c.name}`);
      assert.strictEqual(out.code, c.expectCode, `code mismatch for ${c.name}`);
    });
  }
});
