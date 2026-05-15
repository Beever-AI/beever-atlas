/**
 * AcronymLegendModule — compact two-column legend of glossary terms
 * that ACTUALLY appear on this page. Each row shows the term in mono
 * + accent color and the definition (truncated to 120 chars).
 *
 * Rendered near the bottom of the page so readers resolve unfamiliar
 * acronyms after working through the prose; placing it at the top
 * would waste header real estate.
 *
 * Shape (set by `wiki/modules/acronym_legend.py::build_acronym_legend_data`):
 *   - `items`: array of {term, definition, first_mentioned_by}
 */

import type { ModuleProps } from "./ModuleRenderer";
import { truncateAtSentence } from "@/lib/textTruncate";

interface LegendItem {
  term?: string;
  definition?: string;
  first_mentioned_by?: string;
}

interface AcronymLegendData {
  label?: string;
  items?: LegendItem[];
}

const DEFINITION_MAX = 120;

/** Truncate definition to ~120 chars, preferring sentence/word
 *  boundaries via the shared ``truncateAtSentence`` helper. */
function truncateDefinition(s: string): string {
  return truncateAtSentence(s, DEFINITION_MAX);
}

export function AcronymLegendModule({ module }: ModuleProps) {
  const data = (module.data ?? {}) as AcronymLegendData;
  const items = data.items ?? [];

  if (items.length === 0) return null;

  return (
    <section
      className="mt-8 rounded-lg border border-border/60 bg-muted/10 p-4"
      id={`module-${module.anchor}`}
      data-testid="module-acronym_legend"
      data-toc-skip
    >
      <h2 className="text-sm font-semibold text-foreground mb-3 inline-flex items-center gap-2">
        <span aria-hidden="true">📖</span>
        <span>Terms used on this page</span>
      </h2>
      <dl
        className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-2 text-sm"
        data-testid="acronym-legend-list"
      >
        {items.map((item, idx) => {
          const term = (item.term || "").trim();
          if (!term) return null;
          return (
            <div
              key={`${term}-${idx}`}
              className="flex items-baseline gap-2"
              data-testid="acronym-legend-row"
            >
              <dt
                data-testid="acronym-legend-term"
                className="font-mono text-blue-600 dark:text-blue-400 text-xs whitespace-nowrap shrink-0"
              >
                {term}
              </dt>
              <dd
                data-testid="acronym-legend-definition"
                className="text-muted-foreground text-xs leading-snug"
              >
                {truncateDefinition(item.definition || "")}
              </dd>
            </div>
          );
        })}
      </dl>
    </section>
  );
}
