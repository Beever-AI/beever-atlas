/**
 * Regression tests for the SSRF host allowlist on TeamsBridge.proxyFile
 * (CodeQL alert #30, js/request-forgery, critical).
 *
 * Microsoft Teams attachments are served from Microsoft Graph
 * (`graph.microsoft.com`) and per-tenant SharePoint subdomains
 * (`<tenant>.sharepoint.com`, `<tenant>-my.sharepoint.com`). The bot does
 * NOT attach a token in this fetch (the URL is signed by MS Graph), so
 * the SSRF risk is amplification / internal-network probing. Still, the
 * existing `assertPublicUrl` is too loose — it accepts any public host.
 *
 * The fix runs the URL through `assertAllowedFetchUrl(url, TEAMS_FILE_HOSTS)`
 * before the fetch. We test the constant directly — wiring is verified by
 * `tsc` + the existing `npm test` suite + a grep audit in code review.
 */

import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { assertAllowedFetchUrl, isHostAllowed } from "./bridge.js";

const TEAMS_FILE_HOSTS = ["graph.microsoft.com", ".sharepoint.com"] as const;

describe("assertAllowedFetchUrl — Teams file host allowlist (alert #30)", () => {
  it("accepts canonical Microsoft Graph URL", async () => {
    await assert.doesNotReject(() =>
      assertAllowedFetchUrl(
        "https://graph.microsoft.com/v1.0/me/drive/items/abc/content",
        TEAMS_FILE_HOSTS,
      ),
    );
  });

  it("accepts per-tenant SharePoint subdomain (pure matcher; no DNS)", () => {
    // Tested via `isHostAllowed` to avoid DNS dependency on synthetic tenant
    // names like `contoso.sharepoint.com` that don't actually resolve.
    assert.equal(isHostAllowed("contoso.sharepoint.com", TEAMS_FILE_HOSTS), true);
    assert.equal(isHostAllowed("acme-corp.sharepoint.com", TEAMS_FILE_HOSTS), true);
  });

  it("accepts OneDrive-style `<tenant>-my.sharepoint.com` subdomain (pure matcher)", () => {
    assert.equal(isHostAllowed("contoso-my.sharepoint.com", TEAMS_FILE_HOSTS), true);
  });

  it("rejects unrelated public host (core SSRF case)", async () => {
    await assert.rejects(
      () => assertAllowedFetchUrl("https://attacker.com/x", TEAMS_FILE_HOSTS),
      /not in allowlist/i,
    );
  });

  it("rejects substring-bypass `graph.microsoft.com.evil.com`", async () => {
    await assert.rejects(
      () =>
        assertAllowedFetchUrl(
          "https://graph.microsoft.com.evil.com/v1.0/me",
          TEAMS_FILE_HOSTS,
        ),
      /not in allowlist/i,
    );
  });

  it("rejects substring-bypass `evil.com/sharepoint.com`", async () => {
    await assert.rejects(
      () =>
        assertAllowedFetchUrl("https://evil.com/sharepoint.com/foo", TEAMS_FILE_HOSTS),
      /not in allowlist/i,
    );
  });

  it("rejects `sharepoint.com` apex (dot-prefix requires proper subdomain)", () => {
    // SharePoint apex is not a tenant — only `<tenant>.sharepoint.com`
    // serves user content. Apex must NOT match.
    assert.equal(isHostAllowed("sharepoint.com", TEAMS_FILE_HOSTS), false);
    assert.equal(isHostAllowed("contoso.sharepoint.com", TEAMS_FILE_HOSTS), true);
  });

  it("rejects deceptive `sharepoint.com.evil.com`", async () => {
    await assert.rejects(
      () =>
        assertAllowedFetchUrl("https://contoso.sharepoint.com.evil.com/x", TEAMS_FILE_HOSTS),
      /not in allowlist/i,
    );
  });

  it("rejects `outlook.office.com` and `office.net` (NOT in current allowlist)", async () => {
    // Documents the boundary: only Graph + SharePoint are allowed today.
    // Other Microsoft hosts (office.net, officeapps.live.com, attachments.
    // office.net, etc.) require explicit allowlist entries before they can
    // be proxied — fail-closed behavior.
    await assert.rejects(
      () =>
        assertAllowedFetchUrl("https://outlook.office.com/api/x", TEAMS_FILE_HOSTS),
      /not in allowlist/i,
    );
    await assert.rejects(
      () =>
        assertAllowedFetchUrl("https://attachments.office.net/file/x", TEAMS_FILE_HOSTS),
      /not in allowlist/i,
    );
  });
});
