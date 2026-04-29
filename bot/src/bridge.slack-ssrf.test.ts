/**
 * Regression tests for the SSRF host allowlist on SlackBridge.proxyFile
 * (CodeQL alert #27, js/request-forgery, critical).
 *
 * The fix layers `assertAllowedFetchUrl` on top of `assertPublicUrl`:
 * `assertPublicUrl` blocks private-IP / loopback / cloud-metadata destinations,
 * but accepts any public host. For Slack-bot-token-bearing fetches that's
 * insufficient — an attacker who can route any URL through the file proxy
 * would otherwise leak the bot token to a public host they control.
 *
 * We test `assertAllowedFetchUrl` directly (the function carries the security
 * behavior). Wiring inside `_proxyFileInner` is verified by `tsc` + the
 * existing `npm test` suite + a grep-based audit in code review.
 *
 * Coverage (10 cases):
 *   1. Accepts canonical files.slack.com URL
 *   2. Accepts legacy slack-files.com URL
 *   3. Rejects unrelated public host (the core SSRF case)
 *   4. Rejects substring-bypass `files.slack.com.evil.com`
 *   5. Rejects substring-bypass `evil.com/files.slack.com`
 *   6. Rejects api.slack.com (different Slack endpoint, not a file host)
 *   7. Rejects private-IP destination even if allowlisted host
 *   8. Rejects unsupported scheme (ftp://files.slack.com)
 *   9. Returns parsed URL on success (sanity check for downstream callers)
 *  10. Subdomain isolation: plain entry does NOT match `sub.files.slack.com`
 */

import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { assertAllowedFetchUrl, isHostAllowed } from "./bridge.js";

const SLACK_FILE_HOSTS = ["files.slack.com", "slack-files.com"] as const;

describe("assertAllowedFetchUrl — Slack file host allowlist", () => {
  it("accepts canonical files.slack.com URL", async () => {
    await assert.doesNotReject(() =>
      assertAllowedFetchUrl(
        "https://files.slack.com/files-pri/T0APJ2FNUKZ-F0123456789/screenshot.png",
        SLACK_FILE_HOSTS,
      ),
    );
  });

  it("accepts legacy slack-files.com URL", async () => {
    await assert.doesNotReject(() =>
      assertAllowedFetchUrl("https://slack-files.com/T123/F456/some-file", SLACK_FILE_HOSTS),
    );
  });

  it("rejects unrelated public host (core SSRF case)", async () => {
    await assert.rejects(
      () => assertAllowedFetchUrl("https://attacker.com/exfil", SLACK_FILE_HOSTS),
      /not in allowlist/i,
    );
  });

  it("rejects substring-bypass with allowlisted host as subdomain prefix", async () => {
    await assert.rejects(
      () =>
        assertAllowedFetchUrl(
          "https://files.slack.com.evil.com/files-pri/T1-F2/x.png",
          SLACK_FILE_HOSTS,
        ),
      /not in allowlist/i,
    );
  });

  it("rejects substring-bypass with allowlisted host in path", async () => {
    await assert.rejects(
      () => assertAllowedFetchUrl("https://evil.com/files.slack.com/foo", SLACK_FILE_HOSTS),
      /not in allowlist/i,
    );
  });

  it("rejects api.slack.com (different Slack endpoint, not a file host)", async () => {
    // The Slack bot token grants Web API access too — but this PR scopes the
    // proxy strictly to file hosts. Other endpoints must NOT pass.
    await assert.rejects(
      () => assertAllowedFetchUrl("https://api.slack.com/api/auth.test", SLACK_FILE_HOSTS),
      /not in allowlist/i,
    );
  });

  it("rejects private-IP literal even though it would never match the allowlist", async () => {
    // Private-IP rejection is layered: assertPublicUrl runs first and rejects
    // with "private IP" error before host comparison ever happens.
    await assert.rejects(
      () => assertAllowedFetchUrl("http://127.0.0.1/files-pri/x", SLACK_FILE_HOSTS),
      /private IP|127\./i,
    );
  });

  it("rejects unsupported scheme (ftp://files.slack.com)", async () => {
    await assert.rejects(
      () => assertAllowedFetchUrl("ftp://files.slack.com/x", SLACK_FILE_HOSTS),
      /unsupported scheme/i,
    );
  });

  it("returns parsed URL on success", async () => {
    const parsed = await assertAllowedFetchUrl(
      "https://files.slack.com/files-pri/T1-F2/x.png?t=abc",
      SLACK_FILE_HOSTS,
    );
    assert.equal(parsed.hostname, "files.slack.com");
    assert.equal(parsed.pathname, "/files-pri/T1-F2/x.png");
    assert.equal(parsed.searchParams.get("t"), "abc");
  });

  it("plain allowlist entry does not match an arbitrary subdomain", async () => {
    // `files.slack.com` in the allowlist must not authorize `sub.files.slack.com`.
    // To allow a subdomain tree, callers must pass an entry prefixed with ".".
    await assert.rejects(
      () => assertAllowedFetchUrl("https://sub.files.slack.com/x", SLACK_FILE_HOSTS),
      /not in allowlist/i,
    );
  });

  it("dot-prefixed allowlist entry matches proper subdomains but not the bare host", () => {
    // Sanity-check the suffix-match semantics so future per-platform
    // allowlists (e.g. ".sharepoint.com" for Teams) behave as documented.
    // Tested via the pure `isHostAllowed` matcher to avoid DNS dependency.
    const HOSTS = [".example.com"] as const;
    assert.equal(isHostAllowed("a.example.com", HOSTS), true);
    assert.equal(isHostAllowed("deep.sub.example.com", HOSTS), true);
    assert.equal(isHostAllowed("example.com", HOSTS), false);
    assert.equal(isHostAllowed("a.example.com", ["example.com"]), false);
    // Case-insensitive matching.
    assert.equal(isHostAllowed("A.Example.COM", HOSTS), true);
  });
});
