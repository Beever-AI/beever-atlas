/**
 * Regression tests for the SSRF guard on MattermostBridge.proxyFile (issue #27).
 *
 * The fix adds `decodeURIComponent` + `await assertPublicUrl(decodedUrl)` to
 * MattermostBridge.proxyFile, matching the pattern used by Slack/Discord/
 * Teams/Telegram. We test `assertPublicUrl` directly (the function is
 * exported as part of this fix) rather than instantiating MattermostBridge,
 * because the security behavior lives in the validator. The proxyFile ->
 * assertPublicUrl wiring is verified by an `awk` grep + tsc compile + the
 * existing `npm test` suite.
 *
 * Coverage (10 cases):
 *   1. IPv4 cloud metadata (169.254.169.254)
 *   2. IPv4 loopback (127.0.0.1)
 *   3. IPv4 RFC1918 (10.0.0.1)
 *   4. URL-encoded bypass (decode-then-validate ordering contract)
 *   5. Public URL pass (IP literal, no DNS)
 *   6. Composed-baseUrl-with-private-IP
 *   7. Non-http scheme (ftp://)
 *   8. IPv6 loopback ([::1])
 *   9. IPv6 link-local ([fe80::1])
 *  10. IPv4-mapped IPv6 ([::ffff:127.0.0.1])
 */

import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { assertPublicUrl, assertAllowedFetchUrl, isHostAllowed } from "./bridge.js";

describe("assertPublicUrl — SSRF guard", () => {
  it("rejects IPv4 cloud metadata (169.254.169.254)", async () => {
    await assert.rejects(
      () => assertPublicUrl("http://169.254.169.254/latest/meta-data/iam/security-credentials/"),
      /private IP|169\.254/i,
    );
  });

  it("rejects IPv4 loopback (127.0.0.1)", async () => {
    await assert.rejects(
      () => assertPublicUrl("http://127.0.0.1/secret"),
      /private IP|127\./i,
    );
  });

  it("rejects IPv4 RFC1918 (10.0.0.1)", async () => {
    await assert.rejects(
      () => assertPublicUrl("http://10.0.0.1/internal"),
      /private IP|10\./i,
    );
  });

  it("rejects URL-encoded bypass after decode (ordering contract)", async () => {
    // Documents the decode-then-validate ordering contract;
    // after decodeURIComponent, input equals case 1.
    const decoded = decodeURIComponent("http://169.254.169.254%2flatest");
    await assert.rejects(
      () => assertPublicUrl(decoded),
      /private IP|169\.254/i,
    );
  });

  it("permits public IP literal (no DNS dependency)", async () => {
    // 8.8.8.8 is a public IP literal; isIP() returns 4, isPrivateIP() returns false,
    // so assertPublicUrl resolves without DNS lookup.
    await assert.doesNotReject(() => assertPublicUrl("https://8.8.8.8/test"));
  });

  it("rejects composed-baseUrl with private IP", async () => {
    // Mattermost's proxyFile composes ${baseUrl}${url} when url is a relative
    // path. If baseUrl resolves to a private IP, the composed URL must still
    // be rejected.
    const composed = decodeURIComponent("http://10.0.0.5" + "/api/v4/files/abc");
    await assert.rejects(
      () => assertPublicUrl(composed),
      /private IP|10\./i,
    );
  });

  it("rejects non-http scheme (ftp://)", async () => {
    await assert.rejects(
      () => assertPublicUrl("ftp://internal.corp/"),
      /unsupported scheme/i,
    );
  });

  it("rejects IPv6 loopback ([::1])", async () => {
    await assert.rejects(() => assertPublicUrl("http://[::1]/secret"));
  });

  it("rejects IPv6 link-local ([fe80::1])", async () => {
    await assert.rejects(() => assertPublicUrl("http://[fe80::1]/secret"));
  });

  it("rejects IPv4-mapped IPv6 ([::ffff:127.0.0.1])", async () => {
    await assert.rejects(() => assertPublicUrl("http://[::ffff:127.0.0.1]/secret"));
  });
});

/**
 * Per-instance allowlist for MattermostBridge (CodeQL alert #32).
 *
 * `assertPublicUrl` blocks private-IP destinations but accepts any public
 * host. The Mattermost bot token is per-instance, so cross-instance
 * pivots (instance A's bot token sent to instance B) must be rejected
 * even if instance B is public. The bridge derives its allowlist from
 * `baseUrl`'s hostname at construction; we exercise that derivation
 * shape via the underlying matcher.
 */
describe("MattermostBridge — per-instance host allowlist (alert #32)", () => {
  it("instance allowlist accepts the configured baseUrl host (exact match)", () => {
    // Simulates `new URL(baseUrl).hostname` → single-entry allowlist.
    const allowed = ["mm.acme.example"] as const;
    assert.equal(isHostAllowed("mm.acme.example", allowed), true);
  });

  it("instance allowlist rejects a sibling instance on a different host", () => {
    const allowed = ["mm.acme.example"] as const;
    assert.equal(isHostAllowed("mm.attacker.example", allowed), false);
    assert.equal(isHostAllowed("acme.example", allowed), false);
    assert.equal(isHostAllowed("a.mm.acme.example", allowed), false);
  });

  it("rejects substring-bypass against a configured baseUrl host", async () => {
    await assert.rejects(
      () =>
        assertAllowedFetchUrl(
          "https://mm.acme.example.evil.com/api/v4/files/abc",
          ["mm.acme.example"],
        ),
      /not in allowlist/i,
    );
  });

  it("instance allowlist is case-insensitive", () => {
    const allowed = ["mm.acme.example"] as const;
    assert.equal(isHostAllowed("MM.ACME.example", allowed), true);
  });

  it("composed-baseUrl with relative path resolves against the configured host", async () => {
    // proxyFile composes `${baseUrl}${url}` when url is a relative path
    // (e.g. `/api/v4/files/abc`). The composed URL must still hostname-
    // match the per-instance allowlist.
    const composed = "https://mm.acme.example" + "/api/v4/files/abc";
    // assertAllowedFetchUrl follows the same parse + allowlist sequence
    // proxyFile uses; rejection here is via the (synthetic-host) DNS path,
    // but allowlist match runs first so the regex is "not in allowlist"
    // for any non-allowlisted host.
    await assert.rejects(
      () => assertAllowedFetchUrl(composed, ["other.host.example"]),
      /not in allowlist/i,
    );
  });
});
