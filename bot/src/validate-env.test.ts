/**
 * Tests for detectEnvWarnings (issue #53).
 *
 * The function is WARN-only by design — these tests assert the right
 * conditions trigger the right warning lines, and that the bot's
 * tolerant-defaults philosophy survives (clean dev env produces only
 * the warnings the operator can act on).
 */

import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { detectEnvWarnings } from "./validate-env.js";

const FULL_ENV = {
  BACKEND_URL: "http://backend:8000",
  REDIS_URL: "redis://redis:6379",
  BEEVER_API_KEYS: "k1",
  BRIDGE_API_KEY: "bridge-key",
  BEEVER_ENV: "production",
} as const;

describe("detectEnvWarnings", () => {
  it("returns no warnings when every critical var is set", () => {
    assert.deepEqual(detectEnvWarnings(FULL_ENV), []);
  });

  it("warns on missing BEEVER_API_KEYS in dev (not gated by mode)", () => {
    const warnings = detectEnvWarnings({
      BACKEND_URL: "http://localhost:8000",
      REDIS_URL: "redis://localhost:6379",
      BEEVER_API_KEYS: "",
      BEEVER_ENV: "development",
    });
    assert.equal(warnings.length, 1);
    assert.match(warnings[0], /BEEVER_API_KEYS is empty/);
  });

  it("treats whitespace-only BEEVER_API_KEYS as empty", () => {
    const warnings = detectEnvWarnings({ ...FULL_ENV, BEEVER_API_KEYS: "  ,  ,  " });
    assert.equal(warnings.length, 1);
    assert.match(warnings[0], /BEEVER_API_KEYS is empty/);
  });

  it("accepts BEEVER_API_KEYS with at least one non-empty entry", () => {
    const warnings = detectEnvWarnings({ ...FULL_ENV, BEEVER_API_KEYS: ",,real-key,," });
    assert.deepEqual(warnings, []);
  });

  it("warns on missing BRIDGE_API_KEY in production only", () => {
    const prodWarn = detectEnvWarnings({ ...FULL_ENV, BRIDGE_API_KEY: "" });
    assert.equal(prodWarn.length, 1);
    assert.match(prodWarn[0], /BRIDGE_API_KEY is empty in production/);

    const devNoWarn = detectEnvWarnings({
      ...FULL_ENV,
      BRIDGE_API_KEY: "",
      BEEVER_ENV: "development",
    });
    assert.deepEqual(devNoWarn, []);

    const stagingNoWarn = detectEnvWarnings({
      ...FULL_ENV,
      BRIDGE_API_KEY: "",
      BEEVER_ENV: "staging",
    });
    assert.deepEqual(stagingNoWarn, []);
  });

  it("falls back to NODE_ENV when BEEVER_ENV is unset", () => {
    const warnings = detectEnvWarnings({
      ...FULL_ENV,
      BEEVER_ENV: undefined,
      NODE_ENV: "production",
      BRIDGE_API_KEY: "",
    });
    assert.equal(warnings.length, 1);
    assert.match(warnings[0], /BRIDGE_API_KEY is empty in production/);
  });

  it("warns on unparseable BACKEND_URL", () => {
    const warnings = detectEnvWarnings({ ...FULL_ENV, BACKEND_URL: "not a url" });
    assert.equal(warnings.length, 1);
    assert.match(warnings[0], /BACKEND_URL=not a url is not a valid URL/);
  });

  it("warns on unexpected BACKEND_URL scheme", () => {
    const warnings = detectEnvWarnings({ ...FULL_ENV, BACKEND_URL: "ftp://backend" });
    assert.equal(warnings.length, 1);
    assert.match(warnings[0], /BACKEND_URL=ftp:\/\/backend has unexpected scheme 'ftp:'/);
  });

  it("warns on unexpected REDIS_URL scheme", () => {
    const warnings = detectEnvWarnings({ ...FULL_ENV, REDIS_URL: "http://oops:6379" });
    assert.equal(warnings.length, 1);
    assert.match(warnings[0], /REDIS_URL=http:\/\/oops:6379 has unexpected scheme 'http:'/);
  });

  it("accepts rediss:// (TLS) for REDIS_URL", () => {
    const warnings = detectEnvWarnings({ ...FULL_ENV, REDIS_URL: "rediss://tls-redis:6380" });
    assert.deepEqual(warnings, []);
  });

  it("accepts https:// for BACKEND_URL", () => {
    const warnings = detectEnvWarnings({ ...FULL_ENV, BACKEND_URL: "https://api.example.com" });
    assert.deepEqual(warnings, []);
  });

  it("aggregates multiple independent warnings", () => {
    const warnings = detectEnvWarnings({
      BACKEND_URL: "ftp://wrong",
      REDIS_URL: "garbage",
      BEEVER_API_KEYS: "",
      BRIDGE_API_KEY: "",
      BEEVER_ENV: "production",
    });
    assert.equal(warnings.length, 4);
  });
});
