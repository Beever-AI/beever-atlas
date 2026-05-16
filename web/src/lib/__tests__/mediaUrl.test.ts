import { describe, it, expect } from "vitest";
import {
  needsAuthProxy,
  mediaProxyPathFor,
  filesProxyPathFor,
  isAuthGatedMediaUrl,
} from "@/lib/mediaUrl";

describe("needsAuthProxy", () => {
  it("returns true for Slack file URLs", () => {
    expect(needsAuthProxy("https://files.slack.com/files-pri/T0/F1/x.png")).toBe(true);
  });

  it("returns true for Mattermost cloud URLs", () => {
    expect(
      needsAuthProxy("https://files.mattermost.com/api/v4/files/abc"),
    ).toBe(true);
  });

  it("returns true for self-hosted Mattermost via /api/v4/files/ path", () => {
    // The user's actual host (`team.votee.com`) — covered by the
    // path-prefix rule so we don't need per-tenant frontend config.
    expect(
      needsAuthProxy("https://team.votee.com/api/v4/files/8u7jq3rf"),
    ).toBe(true);
    expect(
      needsAuthProxy("https://chat.example.org/api/v4/files/xyz"),
    ).toBe(true);
  });

  it("returns true for Discord CDN URLs", () => {
    expect(
      needsAuthProxy("https://cdn.discordapp.com/attachments/1/2/x.jpg"),
    ).toBe(true);
  });

  it("returns false for plain public URLs", () => {
    expect(needsAuthProxy("https://github.com/foo/bar")).toBe(false);
    expect(needsAuthProxy("https://example.com/x.png")).toBe(false);
    expect(needsAuthProxy("https://www.youtube.com/watch?v=abc")).toBe(false);
  });

  it("rejects substring-style host smuggling (CodeQL #19/#20)", () => {
    // `evil.com/files.slack.com` must NOT count as a Slack file —
    // we compare parsed hostname, not raw URL substring.
    expect(needsAuthProxy("https://evil.com/files.slack.com")).toBe(false);
    expect(
      needsAuthProxy("https://attacker.com/x?ref=files.slack.com"),
    ).toBe(false);
  });

  it("does NOT match a path that only mentions /api/v4/files/ as a query value", () => {
    // The check is on `pathname.startsWith()`, so query/hash values
    // can't smuggle a Mattermost-shape match.
    expect(
      needsAuthProxy("https://attacker.com/x?path=/api/v4/files/abc"),
    ).toBe(false);
  });

  it("returns false for malformed URLs", () => {
    expect(needsAuthProxy("not a url")).toBe(false);
    expect(needsAuthProxy("")).toBe(false);
    expect(needsAuthProxy(undefined)).toBe(false);
  });
});

describe("mediaProxyPathFor / filesProxyPathFor", () => {
  // EE-side patch: mediaProxyPathFor now routes to /api/files/proxy (same
  // as filesProxyPathFor) because the /api/media/proxy signed-token path
  // returns 502 "Upstream returned 401" when LOADER_TOKEN_SECRET is empty
  // and BEEVER_LOADER_RAW_KEY_FALLBACK=true. The /api/files/proxy raw-key
  // path is the working route for Mattermost-bot-gated files.
  it("returns the /api/files/proxy path for proxied URLs", () => {
    const url = "https://team.votee.com/api/v4/files/abc";
    expect(mediaProxyPathFor(url)).toBe(
      `/api/files/proxy?url=${encodeURIComponent(url)}`,
    );
  });

  it("returns the /api/files/proxy path for the legacy wiki loader", () => {
    const url = "https://files.slack.com/files-pri/T0/F1/x.png";
    expect(filesProxyPathFor(url)).toBe(
      `/api/files/proxy?url=${encodeURIComponent(url)}`,
    );
  });

  it("returns undefined when proxying is not needed", () => {
    expect(mediaProxyPathFor("https://github.com/x")).toBeUndefined();
    expect(filesProxyPathFor("https://github.com/x")).toBeUndefined();
  });

  it("returns undefined for empty / undefined inputs", () => {
    expect(mediaProxyPathFor(undefined)).toBeUndefined();
    expect(filesProxyPathFor("")).toBeUndefined();
  });
});

describe("isAuthGatedMediaUrl (existing — coverage check)", () => {
  it("flags graph.microsoft.com as auth-gated", () => {
    expect(isAuthGatedMediaUrl("https://graph.microsoft.com/v1/me")).toBe(true);
  });
});
