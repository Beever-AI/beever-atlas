/**
 * FolderStatsModule — 4-card big-number strip on folder index pages.
 *
 * Shape (set by `wiki/modules/folder_stats.py::build_folder_stats_data`):
 *   - `stats`: array of {value, label} (4 cards: memories, decisions,
 *              open questions, contributors)
 *   - `subpage_count`: total descendant pages (telemetry only)
 *
 * Visual: 4-column responsive grid (4 → 2 → 1 cards as the viewport
 * narrows). Each card carries a folder icon, the big-number value
 * (24px tabular-nums), and an uppercase label below. Visually
 * distinct from `StatStripModule` (folder icon + slightly tighter
 * spacing) so readers can tell folder-aggregate stats apart from
 * topic-aggregate stats.
 */

import { FolderTree } from "lucide-react";
import type { ModuleProps } from "./ModuleRenderer";

interface StatItem {
  value?: string;
  label?: string;
}

interface FolderStatsData {
  label?: string;
  stats?: StatItem[];
  subpage_count?: number;
}

export function FolderStatsModule({ module }: ModuleProps) {
  const data = (module.data ?? {}) as FolderStatsData;
  const stats = data.stats ?? [];

  if (stats.length === 0) return null;

  return (
    <section
      className="mt-4 mb-6"
      id={`module-${module.anchor}`}
      data-testid="module-folder_stats"
      data-toc-skip
    >
      <ul
        className="grid grid-cols-2 sm:grid-cols-2 lg:grid-cols-4 gap-3 list-none"
        data-testid="folder-stats-list"
      >
        {stats.map((stat, idx) => {
          const value = (stat.value || "").trim();
          const label = (stat.label || "").trim();
          if (!value && !label) return null;
          return (
            <li
              key={`${label}-${idx}`}
              className="rounded-lg border border-border/60 bg-muted/10 px-4 py-3 flex flex-col items-start"
              data-testid="folder-stat-card"
            >
              <span
                aria-hidden="true"
                className="text-muted-foreground/60 mb-1.5"
              >
                <FolderTree size={12} />
              </span>
              <span
                data-testid="folder-stat-value"
                className="text-2xl font-semibold text-foreground tracking-tight tabular-nums"
              >
                {value || "0"}
              </span>
              {label && (
                <span
                  data-testid="folder-stat-label"
                  className="mt-1 text-[11px] uppercase tracking-wider text-muted-foreground"
                >
                  {label}
                </span>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
