/**
 * Regression tests for the SSRF host allowlist on TelegramBridge.proxyFile
 * (CodeQL alert #31, js/request-forgery, critical).
 *
 * Telegram bot file URLs embed the bot token in the path:
 *   `https://api.telegram.org/file/bot<TOKEN>/<path>`
 * If SSRF redirected this fetch to an attacker-controlled host, the token
 * would leak via the request line / Referer / access logs on the
 * attacker's server. This raises the severity here above the other
 * unauthenticated proxy fetches.
 *
 * Telegram serves file content only at `api.telegram.org`. Single-host
 * exact-match allowlist.
 */

import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { assertAllowedFetchUrl, isHostAllowed } from "./bridge.js";

const TELEGRAM_FILE_HOSTS = ["api.telegram.org"] as const;

describe("assertAllowedFetchUrl — Telegram file host allowlist (alert #31)", () => {
  it("accepts canonical Telegram file URL", async () => {
    await assert.doesNotReject(() =>
      assertAllowedFetchUrl(
        "https://api.telegram.org/file/bot12345:secret/photos/file_0.jpg",
        TELEGRAM_FILE_HOSTS,
      ),
    );
  });

  it("rejects unrelated public host (token-leak SSRF case)", async () => {
    // The bot token is in the URL path — if SSRF rerouted this, the token
    // would leak. Most critical case for this allowlist.
    await assert.rejects(
      () =>
        assertAllowedFetchUrl(
          "https://attacker.com/file/bot12345:secret/photos/x.jpg",
          TELEGRAM_FILE_HOSTS,
        ),
      /not in allowlist/i,
    );
  });

  it("rejects substring-bypass `api.telegram.org.evil.com`", async () => {
    await assert.rejects(
      () =>
        assertAllowedFetchUrl(
          "https://api.telegram.org.evil.com/file/bot12345:secret/x.jpg",
          TELEGRAM_FILE_HOSTS,
        ),
      /not in allowlist/i,
    );
  });

  it("rejects substring-bypass `evil.com/api.telegram.org`", async () => {
    await assert.rejects(
      () =>
        assertAllowedFetchUrl(
          "https://evil.com/api.telegram.org/file/x.jpg",
          TELEGRAM_FILE_HOSTS,
        ),
      /not in allowlist/i,
    );
  });

  it("rejects sibling Telegram hosts (`web.telegram.org`, `core.telegram.org`)", () => {
    // Only `api.telegram.org` serves bot file content. Web client and docs
    // hosts must NOT match — they share the suffix but not the role.
    assert.equal(isHostAllowed("web.telegram.org", TELEGRAM_FILE_HOSTS), false);
    assert.equal(isHostAllowed("core.telegram.org", TELEGRAM_FILE_HOSTS), false);
    assert.equal(isHostAllowed("api.telegram.org", TELEGRAM_FILE_HOSTS), true);
  });

  it("rejects `telegram.org` apex", async () => {
    await assert.rejects(
      () => assertAllowedFetchUrl("https://telegram.org/x", TELEGRAM_FILE_HOSTS),
      /not in allowlist/i,
    );
  });

  it("rejects unsupported scheme (ftp://api.telegram.org)", async () => {
    await assert.rejects(
      () =>
        assertAllowedFetchUrl(
          "ftp://api.telegram.org/file/bot12345:secret/x",
          TELEGRAM_FILE_HOSTS,
        ),
      /unsupported scheme/i,
    );
  });
});
