/**
 * Wiki navigation helpers — the wiki tab uses path-based URLs
 * (`/channels/:id/wiki/:slug`) rather than query-string state. The
 * canonical builder is centralised here so the WikiTab, WikiGraph
 * preview panel, and any future call site stay consistent on URL
 * shape and slug encoding.
 *
 * Other query params (`view`, `lang`, `version`) are preserved across
 * path navigations by ``preserveQueryParams``; the legacy `?page=`
 * query is dead and gets scrubbed on mount.
 */

/**
 * Build a wiki-tab URL for the given channel and optional page slug.
 *
 * - Returns ``/channels/{channelId}/wiki`` when ``slug`` is undefined
 *   or empty (the overview page).
 * - Returns ``/channels/{channelId}/wiki/{encoded slug}`` otherwise.
 *
 * The slug is URL-encoded so unusual characters (spaces, ``/``, ``#``)
 * survive the round-trip without mangling the surrounding path.
 */
export function buildWikiPath(channelId: string, slug?: string): string {
  const base = `/channels/${channelId}/wiki`;
  if (!slug) return base;
  return `${base}/${encodeURIComponent(slug)}`;
}

/**
 * Serialise a ``URLSearchParams`` object into a search-string suffix
 * suitable for ``navigate({ search })``.
 *
 * - Returns ``""`` (empty string) when no params remain after dropping
 *   so callers don't end up with a stray ``?`` on path-only URLs.
 * - Returns ``?key=value&...`` (with leading ``?``) otherwise.
 *
 * Used to scrub the legacy ``?page=`` param while keeping ``?view``,
 * ``?lang``, ``?version`` (and any future query state) intact across
 * the path swap.
 */
export function preserveQueryParams(
  searchParams: URLSearchParams,
  drop?: string[],
): string {
  const next = new URLSearchParams(searchParams);
  if (drop) {
    for (const key of drop) {
      next.delete(key);
    }
  }
  const serialized = next.toString();
  return serialized ? `?${serialized}` : "";
}
