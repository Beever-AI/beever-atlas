/**
 * TopContributorsModule — horizontal strip of contributor chips.
 *
 * Shape (set by `wiki/modules/top_contributors.py::build_top_contributors_data`):
 *   - `items`: array of {name, contribution_count, top_pages: [{title, count}, ...]}
 *
 * Visual: horizontal strip of contributor chips (responsive — wraps
 * to multiple rows on narrow viewports). Each chip carries an
 * initials avatar, name, contribution count, and the top page the
 * contributor was active in (truncated to 30 chars).
 */

import type { ModuleProps } from "./ModuleRenderer";

interface TopPage {
  title?: string;
  count?: number;
}

interface ContributorItem {
  name?: string;
  contribution_count?: number;
  top_pages?: TopPage[];
}

interface TopContributorsData {
  label?: string;
  items?: ContributorItem[];
}

/** Two-letter initials from a contributor's name. Falls back to the
 *  first two characters of the name when there's no space (e.g.
 *  "alan" → "AL"). Returns "?" for empty input. */
function initials(name: string): string {
  const trimmed = name.trim();
  if (!trimmed) return "?";
  const parts = trimmed.split(/\s+/).filter(Boolean);
  if (parts.length >= 2) {
    return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
  }
  return trimmed.slice(0, 2).toUpperCase();
}

/** Truncate a page title to ~30 chars, breaking on a word boundary
 *  when possible so labels read cleanly inside the chip. */
function truncatePageTitle(title: string, max = 30): string {
  if (!title || title.length <= max) return title;
  const slice = title.slice(0, max);
  const lastSpace = slice.lastIndexOf(" ");
  if (lastSpace > max / 2) return slice.slice(0, lastSpace) + "…";
  return slice + "…";
}

export function TopContributorsModule({ module }: ModuleProps) {
  const data = (module.data ?? {}) as TopContributorsData;
  const items = data.items ?? [];

  if (items.length === 0) return null;

  return (
    <section
      className="mt-4 mb-6"
      id={`module-${module.anchor}`}
      data-testid="module-top_contributors"
      data-toc-skip
    >
      <h2 className="text-lg font-semibold text-foreground mb-3">
        Top contributors
      </h2>
      <ul
        className="flex flex-wrap items-stretch gap-3 list-none"
        data-testid="top-contributors-list"
      >
        {items.map((item, idx) => {
          const name = (item.name || "").trim();
          if (!name) return null;
          const count = item.contribution_count ?? 0;
          const topPage = (item.top_pages ?? [])[0];
          const topPageTitle = (topPage?.title || "").trim();
          return (
            <li
              key={`${name}-${idx}`}
              className="flex items-center gap-3 rounded-lg border border-border/60 bg-muted/10 px-3 py-2 min-w-[180px]"
              data-testid="top-contributor-chip"
            >
              <span
                aria-hidden="true"
                className="shrink-0 inline-flex h-8 w-8 items-center justify-center rounded-full bg-primary/10 text-primary text-xs font-semibold"
                data-testid="top-contributor-initials"
              >
                {initials(name)}
              </span>
              <div className="flex-1 min-w-0">
                <div
                  className="text-sm font-semibold text-foreground truncate"
                  data-testid="top-contributor-name"
                >
                  {name}
                </div>
                <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                  <span
                    className="font-semibold tabular-nums text-foreground/80"
                    data-testid="top-contributor-count"
                  >
                    {count}
                  </span>
                  <span>
                    {count === 1 ? "contribution" : "contributions"}
                  </span>
                </div>
                {topPageTitle && (
                  <div
                    className="text-[11px] text-muted-foreground/80 truncate mt-0.5"
                    data-testid="top-contributor-top-page"
                    title={topPageTitle}
                  >
                    {truncatePageTitle(topPageTitle)}
                  </div>
                )}
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
