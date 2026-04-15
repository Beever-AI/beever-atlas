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

/** Hosts that are served through the backend `/api/media/proxy` endpoint
 * because direct browser fetches are blocked (needs bearer auth) or
 * return CORP/CORB headers that stop `<img>` rendering.
 *
 * Slack file URLs need the bot token we store server-side. Discord CDN
 * URLs work directly in most browsers, but are proxied on auth-gated
 * paths where `<img>` ends up blocked. */
const PROXY_HOSTS = [
  "files.slack.com",
  "slack-files.com",
];

/** Rewrite a media URL to go through the backend proxy when it points at
 * a host we can't load directly from the browser. `<img>` tags cannot
 * carry a custom `Authorization` header, so we go through `buildLoaderUrl`
 * which appends `?access_token=` for request-time auth. Leaves unrelated
 * URLs unchanged. */
export function proxiedMediaUrl(url: string | undefined): string | undefined {
  if (!url) return url;
  try {
    const parsed = new URL(url);
    const host = parsed.host.toLowerCase();
    const needsProxy = PROXY_HOSTS.some(
      (h) => host === h || host.endsWith(`.${h}`),
    );
    if (!needsProxy) return url;
    return buildLoaderUrl(`/api/media/proxy?url=${encodeURIComponent(url)}`);
  } catch {
    return url;
  }
}
