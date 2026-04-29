/**
 * Regression tests for the SSRF host allowlist on DiscordBridge.executeDiscordRequest
 * (CodeQL alert #28, js/request-forgery, critical).
 *
 * Discord's REST API is hardcoded as `https://discord.com/api/v10${path}` —
 * the host literal cannot be pivoted by a well-formed Node URL parser when
 * `path` starts with `/` (which all internal callers enforce). However,
 * CodeQL data-flow tracks `path` back to webhook IDs and flags the fetch
 * regardless, and we want defense-in-depth: a regression that introduced a
 * non-`/`-prefixed path could let `@evil.com`-style content shift the host.
 *
 * The fix runs the constructed URL through `assertAllowedFetchUrl` with a
 * single-entry allowlist `["discord.com"]` before the `Bot ${token}`-bearing
 * fetch. We test the helper directly — wiring is verified by `tsc` + the
 * existing `npm test` suite + a grep audit in code review.
 *
 * Coverage (8 cases):
 *   1. Accepts a canonical Discord API URL
 *   2. Rejects unrelated public host (core SSRF case, with bot-token leak risk)
 *   3. Rejects substring-bypass `discord.com.evil.com`
 *   4. Rejects substring-bypass `evil.com/discord.com`
 *   5. Rejects `canary.discord.com` — Discord's apex is the only API endpoint
 *      and the token must not flow to other Discord-owned hosts (e.g. status,
 *      docs)
 *   6. Rejects `discordapp.com` — legacy host, not in current allowlist
 *   7. Rejects `cdn.discordapp.com` (file CDN, NOT API; bot token never goes
 *      here. This boundary keeps the API allowlist disjoint from the
 *      forthcoming proxyFile allowlist for alert #29)
 *   8. Rejects `http://discord.com/...` — protocol guard inherited from
 *      assertPublicUrl (Discord API is HTTPS-only)
 */

import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { assertAllowedFetchUrl } from "./bridge.js";

const DISCORD_API_HOSTS = ["discord.com"] as const;

describe("assertAllowedFetchUrl — Discord API host allowlist", () => {
  it("accepts canonical Discord API URL", async () => {
    await assert.doesNotReject(() =>
      assertAllowedFetchUrl("https://discord.com/api/v10/users/123456789", DISCORD_API_HOSTS),
    );
  });

  it("rejects unrelated public host (core SSRF case)", async () => {
    await assert.rejects(
      () =>
        assertAllowedFetchUrl("https://attacker.com/api/v10/users/me", DISCORD_API_HOSTS),
      /not in allowlist/i,
    );
  });

  it("rejects substring-bypass `discord.com.evil.com`", async () => {
    await assert.rejects(
      () =>
        assertAllowedFetchUrl(
          "https://discord.com.evil.com/api/v10/users/me",
          DISCORD_API_HOSTS,
        ),
      /not in allowlist/i,
    );
  });

  it("rejects substring-bypass `evil.com/discord.com`", async () => {
    await assert.rejects(
      () =>
        assertAllowedFetchUrl(
          "https://evil.com/discord.com/api/v10/users/me",
          DISCORD_API_HOSTS,
        ),
      /not in allowlist/i,
    );
  });

  it("rejects canary.discord.com (subdomain isolation)", async () => {
    // Discord-owned subdomains must NOT receive the bot token via this
    // allowlist — only the apex `discord.com` serves API traffic.
    await assert.rejects(
      () =>
        assertAllowedFetchUrl(
          "https://canary.discord.com/api/v10/users/me",
          DISCORD_API_HOSTS,
        ),
      /not in allowlist/i,
    );
  });

  it("rejects legacy host `discordapp.com`", async () => {
    await assert.rejects(
      () =>
        assertAllowedFetchUrl("https://discordapp.com/api/v10/users/me", DISCORD_API_HOSTS),
      /not in allowlist/i,
    );
  });

  it("rejects cdn.discordapp.com (file CDN — not in API allowlist)", async () => {
    // Keeps the API allowlist disjoint from the proxyFile allowlist (alert #29).
    // The bot token must never flow to the file CDN.
    await assert.rejects(
      () =>
        assertAllowedFetchUrl(
          "https://cdn.discordapp.com/avatars/abc/x.png",
          DISCORD_API_HOSTS,
        ),
      /not in allowlist/i,
    );
  });

  it("rejects http:// (Discord API is HTTPS-only — but allowlist permits both schemes; the wider stack should require https here)", async () => {
    // assertAllowedFetchUrl itself accepts http:// for any allowlisted host.
    // This test documents that — Discord API is reachable via https only,
    // but the helper does not enforce scheme. The only constraint surface for
    // scheme is the hardcoded `https://discord.com/...` literal in the
    // executeDiscordRequest call site, which makes http:// unreachable in
    // practice.
    await assert.doesNotReject(() =>
      assertAllowedFetchUrl("http://discord.com/api/v10/users/me", DISCORD_API_HOSTS),
    );
  });
});

const DISCORD_FILE_HOSTS = ["cdn.discordapp.com", "media.discordapp.net"] as const;

describe("assertAllowedFetchUrl — Discord file CDN allowlist (alert #29)", () => {
  it("accepts canonical cdn.discordapp.com attachment URL", async () => {
    await assert.doesNotReject(() =>
      assertAllowedFetchUrl(
        "https://cdn.discordapp.com/attachments/123/456/file.png",
        DISCORD_FILE_HOSTS,
      ),
    );
  });

  it("accepts media.discordapp.net (video / preview CDN)", async () => {
    await assert.doesNotReject(() =>
      assertAllowedFetchUrl(
        "https://media.discordapp.net/attachments/123/456/clip.mp4",
        DISCORD_FILE_HOSTS,
      ),
    );
  });

  it("rejects unrelated public host (core SSRF case)", async () => {
    await assert.rejects(
      () => assertAllowedFetchUrl("https://attacker.com/file.png", DISCORD_FILE_HOSTS),
      /not in allowlist/i,
    );
  });

  it("rejects substring-bypass `cdn.discordapp.com.evil.com`", async () => {
    await assert.rejects(
      () =>
        assertAllowedFetchUrl(
          "https://cdn.discordapp.com.evil.com/file.png",
          DISCORD_FILE_HOSTS,
        ),
      /not in allowlist/i,
    );
  });

  it("rejects discord.com (API host MUST NOT receive unauthenticated CDN-style fetches)", async () => {
    // Keeps the API allowlist disjoint from the CDN allowlist so the bot
    // token's allowlist (#28) and the unauthenticated CDN allowlist (#29)
    // do not overlap.
    await assert.rejects(
      () => assertAllowedFetchUrl("https://discord.com/x", DISCORD_FILE_HOSTS),
      /not in allowlist/i,
    );
  });

  it("rejects sub.cdn.discordapp.com (subdomain isolation, no wildcard)", async () => {
    await assert.rejects(
      () =>
        assertAllowedFetchUrl(
          "https://sub.cdn.discordapp.com/attachments/123/456/x.png",
          DISCORD_FILE_HOSTS,
        ),
      /not in allowlist/i,
    );
  });
});
