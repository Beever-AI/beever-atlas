import { describe, it } from "node:test";
import assert from "node:assert";
import { classifyPlatformError } from "./bridge/platformError.js";

describe("classifyPlatformError", () => {
  it("maps slack not_in_channel to 403 FORBIDDEN", () => {
    const err = {
      code: "slack_webapi_platform_error",
      data: { error: "not_in_channel" },
    };
    assert.deepStrictEqual(classifyPlatformError(err), {
      status: 403,
      code: "FORBIDDEN",
    });
  });

  it("does NOT classify a stray 'not_in_channel' data.error without the slack code as FORBIDDEN via that branch", () => {
    // Regression for the missing-parens precedence bug: previously
    //   A || B && C
    // short-circuited so a bare data.error = "not_in_channel" without the
    // matching code could still match earlier/later branches inconsistently.
    // With proper parens, the AND clause requires BOTH sides. A foreign code
    // with "not_in_channel" must NOT be caught by that specific clause; if
    // it still resolves to 403 it must be via another branch only — here
    // there is no `msg.includes("forbidden")` / `:403`, so we expect
    // the default 500 INTERNAL.
    const err = {
      code: "some_other_error",
      data: { error: "not_in_channel" },
    };
    const result = classifyPlatformError(err);
    assert.notStrictEqual(result.code, "FORBIDDEN");
  });

  it("classifies channel_not_found as 404", () => {
    const err = { data: { error: "channel_not_found" } };
    assert.deepStrictEqual(classifyPlatformError(err), {
      status: 404,
      code: "NOT_FOUND",
    });
  });

  it("classifies invalid_auth as 403", () => {
    const err = { data: { error: "invalid_auth" } };
    assert.deepStrictEqual(classifyPlatformError(err), {
      status: 403,
      code: "FORBIDDEN",
    });
  });

  it("classifies forbidden in message as 403", () => {
    const err = new Error("Request returned: 403 Forbidden");
    assert.strictEqual(classifyPlatformError(err).status, 403);
  });

  it("handles null and primitive errors without throwing", () => {
    assert.ok(classifyPlatformError(null));
    assert.ok(classifyPlatformError(undefined));
    assert.ok(classifyPlatformError("bare string"));
  });
});
