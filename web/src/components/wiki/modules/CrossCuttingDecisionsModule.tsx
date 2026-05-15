/**
 * CrossCuttingDecisionsModule — vertical list of decisions across a
 * folder's descendant pages.
 *
 * Shape (set by
 * `wiki/modules/cross_cutting_decisions.py::build_cross_cutting_decisions_data`):
 *   - `items`: array of {fact_id, title, decided_by, decided_at,
 *              importance, source_page: {title, slug}}
 *
 * Visual: each item is a card with a 3px severity left-border
 * (critical=red, high=amber, medium=primary, low=muted), the
 * decision title in 14px semibold, then a chip row carrying the
 * author + ISO date + "Source page →" link routing back to the
 * descendant page. Empty fields hide their chip (no placeholder).
 */

import { ArrowRight } from "lucide-react";
import type { ModuleProps } from "./ModuleRenderer";
import { SeverityBadge } from "../SeverityBadge";
import { truncateAtSentence } from "@/lib/textTruncate";

interface SourcePage {
  title?: string;
  slug?: string;
}

interface DecisionItem {
  fact_id?: string;
  title?: string;
  decided_by?: string;
  decided_at?: string;
  importance?: string;
  source_page?: SourcePage;
}

interface CrossCuttingDecisionsData {
  label?: string;
  items?: DecisionItem[];
}

/** Map an importance bucket to the severity colour Tailwind classes
 *  used elsewhere in the wiki (key_facts, decision_banner). */
function severityClasses(importance: string): {
  border: string;
  badge: string;
} {
  const v = (importance || "").trim().toLowerCase();
  if (v === "critical") {
    return {
      border: "border-l-red-500",
      badge: "bg-red-500/10 text-red-600 dark:text-red-400",
    };
  }
  if (v === "high") {
    return {
      border: "border-l-amber-500",
      badge: "bg-amber-500/10 text-amber-600 dark:text-amber-400",
    };
  }
  if (v === "low") {
    return {
      border: "border-l-muted-foreground/40",
      badge: "bg-muted/60 text-muted-foreground",
    };
  }
  // medium / default
  return {
    border: "border-l-primary/60",
    badge: "bg-primary/10 text-primary",
  };
}

/** Format an ISO date (YYYY-MM-DD) as "Mon DD, YYYY". Returns the
 *  raw string when parsing fails so a malformed date renders something. */
function formatDate(iso: string): string {
  if (!iso) return "";
  const m = iso.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return iso;
  const [, y, mm, dd] = m;
  const months = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
  ];
  const monthIdx = parseInt(mm, 10) - 1;
  if (monthIdx < 0 || monthIdx > 11) return iso;
  const day = parseInt(dd, 10);
  if (Number.isNaN(day)) return iso;
  return `${months[monthIdx]} ${day}, ${y}`;
}

export function CrossCuttingDecisionsModule({
  module,
  onNavigate,
}: ModuleProps) {
  const data = (module.data ?? {}) as CrossCuttingDecisionsData;
  const items = data.items ?? [];

  if (items.length === 0) return null;

  return (
    <section
      className="mt-4 mb-6"
      id={`module-${module.anchor}`}
      data-testid="module-cross_cutting_decisions"
      data-toc-skip
    >
      <h2 className="text-lg font-semibold text-foreground mb-3">
        Cross-cutting decisions
      </h2>
      <ul
        className="flex flex-col gap-2 list-none"
        data-testid="cross-cutting-decisions-list"
      >
        {items.map((item, idx) => {
          const title = (item.title || "").trim();
          if (!title) return null;
          const decidedBy = (item.decided_by || "").trim();
          const decidedAt = formatDate((item.decided_at || "").trim());
          const importance = (item.importance || "medium").trim().toLowerCase();
          const sourcePage = item.source_page || {};
          const sourceTitle = (sourcePage.title || "").trim();
          const sourceSlug = (sourcePage.slug || "").trim();
          const sev = severityClasses(importance);
          return (
            <li
              key={`${item.fact_id || title}-${idx}`}
              className={`rounded-md border border-border/60 border-l-[3px] ${sev.border} bg-card pl-3 pr-3 py-2.5`}
              data-testid="cross-cutting-decision-item"
            >
              <div
                className="text-sm font-semibold text-foreground leading-snug"
                data-testid="cross-cutting-decision-title"
              >
                {truncateAtSentence(title, 220)}
              </div>
              <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
                <span
                  className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded ${sev.badge} text-[10.5px] uppercase tracking-wider font-semibold`}
                  data-testid="cross-cutting-decision-importance"
                >
                  <SeverityBadge severity={importance} iconSize={10} showDot={false} />
                  {importance}
                </span>
                {decidedBy && (
                  <span data-testid="cross-cutting-decision-author">
                    by{" "}
                    <span className="font-semibold text-foreground/80">
                      {decidedBy}
                    </span>
                  </span>
                )}
                {decidedAt && (
                  <span data-testid="cross-cutting-decision-date">
                    {decidedAt}
                  </span>
                )}
                {sourceTitle && sourceSlug && (
                  <button
                    type="button"
                    onClick={() => onNavigate?.(`topic-${sourceSlug}`)}
                    className="inline-flex items-center gap-0.5 text-primary hover:underline ml-auto"
                    data-testid="cross-cutting-decision-source-link"
                    title={`Open source page: ${sourceTitle}`}
                  >
                    {sourceTitle}
                    <ArrowRight size={11} />
                  </button>
                )}
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
