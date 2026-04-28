/**
 * Regression tests for bridge auth enforcement (security finding M1).
 *
 * Scenarios from `openspec/changes/res-177-security-priority-fixes/specs/bridge-auth-enforcement/spec.md`:
 *
 * - Missing Authorization header → 401
 * - Wrong token → 401
 * - Correct token → 200 (proceeds to handler)
 * - BRIDGE_API_KEY unset AND BRIDGE_ALLOW_UNAUTH unset → 401
 * - BRIDGE_API_KEY unset AND BRIDGE_ALLOW_UNAUTH="true" AND BEEVER_ENV="development" → allowed + loud warn
 * - BRIDGE_API_KEY unset AND BRIDGE_ALLOW_UNAUTH="true" AND BEEVER_ENV unset/staging → STILL 401 (issue #34)
 * - Non-"true" strings for BRIDGE_ALLOW_UNAUTH ("TRUE", "1", "yes") → still closed
 * - BRIDGE_API_KEY set AND BRIDGE_ALLOW_UNAUTH="true" → key wins (opt-in ignored)
 */

import { describe, it, beforeEach } from "node:test";
import assert from "node:assert/strict";
import type { IncomingMessage, ServerResponse } from "node:http";

import { checkAuth, assertBridgeAuthReady } from "./bridge.js";

type FakeReq = Pick<IncomingMessage, "headers">;

interface FakeRes {
  statusCode: number;
  body: string;
  writeHead(status: number, headers?: Record<string, string>): void;
  end(body?: string): void;
}

function makeRes(): FakeRes {
  return {
    statusCode: 0,
    body: "",
    writeHead(status) {
      this.statusCode = status;
    },
    end(body = "") {
      this.body = body;
    },
  };
}

function makeReq(headers: Record<string, string> = {}): FakeReq {
  return { headers };
}

function resetEnv() {
  delete process.env.BRIDGE_API_KEY;
  delete process.env.BRIDGE_ALLOW_UNAUTH;
  delete process.env.BEEVER_BRIDGE_HMAC_DUAL;
  delete process.env.BEEVER_ENV;
  delete process.env.NODE_ENV;
}

describe("checkAuth — bridge auth enforcement (M1)", () => {
  beforeEach(() => {
    resetEnv();
  });

  it("rejects request with missing Authorization when key is set", () => {
    process.env.BRIDGE_API_KEY = "secret";
    const res = makeRes();
    const ok = checkAuth(makeReq() as IncomingMessage, res as unknown as ServerResponse);
    assert.equal(ok, false);
    assert.equal(res.statusCode, 401);
  });

  it("rejects request with wrong bearer token", () => {
    process.env.BRIDGE_API_KEY = "secret";
    const res = makeRes();
    const ok = checkAuth(
      makeReq({ authorization: "Bearer wrong" }) as IncomingMessage,
      res as unknown as ServerResponse,
    );
    assert.equal(ok, false);
    assert.equal(res.statusCode, 401);
  });

  it("accepts request with correct bearer token", () => {
    process.env.BRIDGE_API_KEY = "secret";
    const res = makeRes();
    const ok = checkAuth(
      makeReq({ authorization: "Bearer secret" }) as IncomingMessage,
      res as unknown as ServerResponse,
    );
    assert.equal(ok, true);
    assert.equal(res.statusCode, 0, "handler must not have written a response");
  });

  it("rejects every request when BRIDGE_API_KEY unset and BRIDGE_ALLOW_UNAUTH unset", () => {
    const res = makeRes();
    const ok = checkAuth(
      makeReq({ authorization: "Bearer anything" }) as IncomingMessage,
      res as unknown as ServerResponse,
    );
    assert.equal(ok, false);
    assert.equal(res.statusCode, 401);
  });

  it("allows request when BRIDGE_API_KEY unset, BRIDGE_ALLOW_UNAUTH=\"true\", AND BEEVER_ENV=\"development\"", () => {
    process.env.BRIDGE_ALLOW_UNAUTH = "true";
    process.env.BEEVER_ENV = "development";
    const res = makeRes();
    const ok = checkAuth(makeReq() as IncomingMessage, res as unknown as ServerResponse);
    assert.equal(ok, true);
  });

  // Issue #34 — BRIDGE_ALLOW_UNAUTH must require an explicit BEEVER_ENV=development
  // marker. Without it, an operator who set the flag on staging or in their CI
  // pipeline would have run the bridge wide-open. The flag is now silently
  // ignored unless dev is the explicit env.
  it("issue #34: BRIDGE_ALLOW_UNAUTH=\"true\" with no BEEVER_ENV → still 401", () => {
    process.env.BRIDGE_ALLOW_UNAUTH = "true";
    // BEEVER_ENV intentionally not set
    const res = makeRes();
    const ok = checkAuth(makeReq() as IncomingMessage, res as unknown as ServerResponse);
    assert.equal(ok, false, "without BEEVER_ENV=development the unauth flag must be ignored");
    assert.equal(res.statusCode, 401);
  });

  it("issue #34: BRIDGE_ALLOW_UNAUTH=\"true\" with BEEVER_ENV=\"staging\" → still 401", () => {
    process.env.BRIDGE_ALLOW_UNAUTH = "true";
    process.env.BEEVER_ENV = "staging";
    const res = makeRes();
    const ok = checkAuth(makeReq() as IncomingMessage, res as unknown as ServerResponse);
    assert.equal(ok, false, "non-development envs must not honor the unauth flag");
    assert.equal(res.statusCode, 401);
  });

  it("issue #34: BRIDGE_ALLOW_UNAUTH=\"true\" with NODE_ENV=\"development\" only → still 401", () => {
    // The gate is BEEVER_ENV specifically, not the generic NODE_ENV.
    process.env.BRIDGE_ALLOW_UNAUTH = "true";
    process.env.NODE_ENV = "development";
    const res = makeRes();
    const ok = checkAuth(makeReq() as IncomingMessage, res as unknown as ServerResponse);
    assert.equal(ok, false, "BEEVER_ENV is the explicit dev marker, NODE_ENV is not");
    assert.equal(res.statusCode, 401);
  });

  for (const variant of ["TRUE", "True", "1", "yes", " true"]) {
    it(`does NOT bypass auth when BRIDGE_ALLOW_UNAUTH=${JSON.stringify(variant)}`, () => {
      process.env.BRIDGE_ALLOW_UNAUTH = variant;
      const res = makeRes();
      const ok = checkAuth(makeReq() as IncomingMessage, res as unknown as ServerResponse);
      assert.equal(ok, false, `opt-in must require exact string "true", got ${variant}`);
      assert.equal(res.statusCode, 401);
    });
  }

  it("key enforcement takes precedence over opt-in", () => {
    process.env.BRIDGE_API_KEY = "secret";
    process.env.BRIDGE_ALLOW_UNAUTH = "true";
    const res = makeRes();
    const ok = checkAuth(
      makeReq({ authorization: "Bearer wrong" }) as IncomingMessage,
      res as unknown as ServerResponse,
    );
    assert.equal(ok, false);
    assert.equal(res.statusCode, 401);

    const res2 = makeRes();
    const ok2 = checkAuth(
      makeReq({ authorization: "Bearer secret" }) as IncomingMessage,
      res2 as unknown as ServerResponse,
    );
    assert.equal(ok2, true);
  });

  it("legacy BEEVER_ENV=development no longer auto-bypasses auth", () => {
    // Regression guard for the M1 fix: pre-M1 behaviour treated any
    // non-production env as "no auth needed" when key was unset. That
    // path is gone.
    process.env.BEEVER_ENV = "development";
    const res = makeRes();
    const ok = checkAuth(
      makeReq({ authorization: "Bearer anything" }) as IncomingMessage,
      res as unknown as ServerResponse,
    );
    assert.equal(ok, false);
    assert.equal(res.statusCode, 401);
  });

  it("NODE_ENV=test alone no longer auto-bypasses auth", () => {
    process.env.NODE_ENV = "test";
    const res = makeRes();
    const ok = checkAuth(
      makeReq({ authorization: "Bearer anything" }) as IncomingMessage,
      res as unknown as ServerResponse,
    );
    assert.equal(ok, false);
    assert.equal(res.statusCode, 401);
  });
});

describe("assertBridgeAuthReady — startup warning for explicit opt-in", () => {
  beforeEach(() => {
    resetEnv();
  });

  it("emits a loud warning when running unauthenticated via opt-in (with BEEVER_ENV=development)", () => {
    process.env.BRIDGE_ALLOW_UNAUTH = "true";
    process.env.BEEVER_ENV = "development";
    const captured: string[] = [];
    const originalWarn = console.warn;
    console.warn = (...args: unknown[]) => {
      captured.push(args.map((a) => String(a)).join(" "));
    };
    try {
      assertBridgeAuthReady();
    } finally {
      console.warn = originalWarn;
    }
    assert.ok(
      captured.some((m) => m.includes("BRIDGE_ALLOW_UNAUTH=true")),
      "expected warning to contain BRIDGE_ALLOW_UNAUTH=true",
    );
    assert.ok(
      captured.some((m) => m.includes("Do NOT use in staging or production")),
      "expected warning to contain a staging/production-use caution",
    );
  });

  // Issue #34 — when the operator sets BRIDGE_ALLOW_UNAUTH=true but
  // doesn't explicitly mark the env as development, the flag is ignored
  // at request time. Surface that loudly at startup so they don't think
  // they've bypassed auth when in fact every request will 401.
  it("issue #34: warns that flag is IGNORED when BEEVER_ENV is not development", () => {
    process.env.BRIDGE_ALLOW_UNAUTH = "true";
    // BEEVER_ENV intentionally not set
    const captured: string[] = [];
    const originalWarn = console.warn;
    console.warn = (...args: unknown[]) => {
      captured.push(args.map((a) => String(a)).join(" "));
    };
    try {
      assertBridgeAuthReady();
    } finally {
      console.warn = originalWarn;
    }
    assert.ok(
      captured.some((m) => m.includes("IGNORED")),
      "expected warning to clearly state the flag is being ignored",
    );
    assert.ok(
      captured.some((m) => m.includes("BEEVER_ENV=development")),
      "expected warning to point at the BEEVER_ENV gate",
    );
  });

  it("stays silent when the bridge key is properly configured", () => {
    process.env.BRIDGE_API_KEY = "secret";
    const captured: string[] = [];
    const originalWarn = console.warn;
    console.warn = (...args: unknown[]) => {
      captured.push(args.map((a) => String(a)).join(" "));
    };
    try {
      assertBridgeAuthReady();
    } finally {
      console.warn = originalWarn;
    }
    assert.equal(
      captured.filter((m) => m.includes("BRIDGE_ALLOW_UNAUTH")).length,
      0,
    );
  });
});
