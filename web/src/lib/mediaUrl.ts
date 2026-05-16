/**
 * Helpers for deciding whether a media URL can be rendered inline as an
 * `<img>`/preview, or whether it needs to fall back to a link card because
 * the source gate the browser out (Slack file CDN, Discord attachments,
 * Teams blobs, etc.).
 */
import { buildLoaderUrl } from "@/lib/api";

/** Hosts whose media endpoints reliably 403 the browser with no available
 * workaround. Pre-blocked so we skip the broken-image flash entirely.
 *
 * Slack / Discord are NOT in this list: Slack `files-pri` URLs load when
 * the user has a Slack session cookie (the same cookie that lets them
 * click the link), and Discord CDN URLs carry signed query params. Both
 * are rendered optimistically and fall back via `<img onError>` if they
 * actually fail in a given browser.
 */
const AUTH_GATED_HOSTS = [
  "graph.microsoft.com",
  "attachments.office.net",
];

export function isAuthGatedMediaUrl(url: string | undefined): boolean {
  if (!url) return false;
  try {
    const host = new URL(url).host.toLowerCase();
    return AUTH_GATED_HOSTS.some((h) => host === h || host.endsWith(`.${h}`));
  } catch {
    return false;
  }
}

export function mediaHostLabel(url: string | undefined): string | null {
  if (!url) return null;
  try {
    return new URL(url).host;
  } catch {
    return null;
  }
}

/** Hosts whose media endpoints need the backend proxy because direct
 * browser fetches are blocked (bot-token auth) or return CORP/CORB
 * headers that stop `<img>` rendering.
 *
 * Slack file URLs need the bot token we store server-side. Mattermost
 * cloud / self-hosted URLs need the user session token. Discord CDN
 * URLs work directly in most browsers but are proxied on auth-gated
 * paths where `<img>` ends up blocked. */
const PROXY_HOSTS = [
  "files.slack.com",
  "slack-files.com",
  "files.mattermost.com",
  "cdn.discordapp.com",
];

/** URL pathname patterns that signal "this URL needs auth proxying"
 * regardless of host. Self-hosted Mattermost uses customer hostnames
 * (e.g. `team.example.com`), but the API path is always
 * `/api/v4/files/<id>`. Matching on path lets us proxy any
 * Mattermost-shaped URL without per-tenant frontend config. */
const PROXY_PATH_PREFIXES = [
  "/api/v4/files/", // Mattermost (cloud + self-hosted)
];

/** Hostnames that appear as additional sources for Mattermost API
 * URLs through the path-prefix rule above; the prefix check covers
 * them but listing them here documents the coverage. */
// const SELF_HOSTED_NOTE = ["team.<your-org>.com", "mattermost.<your-org>.com"];

/** Returns true when this URL needs to flow through the backend proxy
 * for browser display. Common signals: known platform host, OR a
 * platform-specific API path (e.g. Mattermost `/api/v4/files/<id>`). */
export function needsAuthProxy(url: string | undefined): boolean {
  if (!url) return false;
  try {
    const parsed = new URL(url);
    const host = parsed.host.toLowerCase();
    if (PROXY_HOSTS.some((h) => host === h || host.endsWith(`.${h}`))) {
      return true;
    }
    if (PROXY_PATH_PREFIXES.some((p) => parsed.pathname.startsWith(p))) {
      return true;
    }
    return false;
  } catch {
    return false;
  }
}

/** Rewrite a media URL to go through the backend proxy when it points at
 * a host we can't load directly from the browser. `<img>` tags cannot
 * carry a custom `Authorization` header, so we go through `buildLoaderUrl`
 * which appends `?access_token=` for request-time auth. Leaves unrelated
 * URLs unchanged.
 *
 * Issue #89 — this remains the synchronous fallback (raw API key). For
 * new `<img>` rendering, prefer `<ProxiedImage unproxiedUrl mediaPath />`
 * with `mediaProxyPathFor(url)` so the URL carries a signed token. This
 * function is still used by `<a href>` cases that cannot await an async
 * mint. */
export function proxiedMediaUrl(url: string | undefined): string | undefined {
  if (!url) return url;
  const proxyPath = mediaProxyPathFor(url);
  if (!proxyPath) return url;
  return buildLoaderUrl(proxyPath);
}

/** Returns the route path needed to proxy this URL through the backend,
 * or `undefined` if the URL does not need proxying (public / unrecognized
 * host).
 *
 * Routes to `/api/files/proxy` (same endpoint as `filesProxyPathFor`).
 * The newer `/api/media/proxy` endpoint was intended for signed loader
 * tokens, but on deployments where `LOADER_TOKEN_SECRET` is empty its
 * credential-resolution path returns `502 Upstream returned 401` for
 * Mattermost-bot-gated files. `/api/files/proxy` uses the
 * `BEEVER_LOADER_RAW_KEY_FALLBACK=true` raw-key path which resolves the
 * platform_connection's bot credential the same way the wiki view does
 * — that path is verified working (HTTP 200, returns file bytes).
 *
 * EE-side patch — upstream OSS still emits `/api/media/proxy`. Backport
 * via PR once the signed-token path is wired through the Mattermost
 * adapter's credential resolver.
 */
export function mediaProxyPathFor(url: string | undefined): string | undefined {
  if (!needsAuthProxy(url)) return undefined;
  return `/api/files/proxy?url=${encodeURIComponent(url!)}`;
}

/** Returns the legacy `/api/files/proxy` path. Same allowlist as
 * `mediaProxyPathFor` — different endpoint kept for the wiki-side
 * loader path that pre-dates `/api/media/proxy`. Both endpoints
 * validate against the backend `FILE_PROXY_HOST_ALLOWLIST`. */
export function filesProxyPathFor(url: string | undefined): string | undefined {
  if (!needsAuthProxy(url)) return undefined;
  return `/api/files/proxy?url=${encodeURIComponent(url!)}`;
}
