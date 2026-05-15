/**
 * StatStripModule — headline cards surfacing numeric values
 * (counts, currencies, k/M-suffixed metrics) detected in fact text.
 *
 * Horizontal cards, equal width, with the value as the dominant
 * text and a small uppercase label below. Period (date range) shows
 * underneath. Responsive: 4 cards per row on desktop, 2 on tablet,
 * 1 stacked on mobile.
 *
 * Shape (set by `wiki/modules/stat_strip.py::build_stat_strip_data`):
 *   - `stats`: array of {value, label, fact_id, raw_value}
 *   - `period`: {from, to}
 */

import type { ModuleProps } from "./ModuleRenderer";

interface StatItem {
  value?: string;
  label?: string;
  fact_id?: string;
  raw_value?: number | null;
}

interface StatStripData {
  label?: string;
  stats?: StatItem[];
  period?: { from?: string; to?: string };
}

/** Format an ISO date prefix as "Apr 26" — drop the year unless
 *  ``includeYear`` is true. */
function formatDate(s: string, includeYear = false): string {
  if (!s) return "";
  const d = new Date(s + "T00:00:00Z");
  if (Number.isNaN(d.getTime())) return s;
  const month = d.toLocaleString("en-US", { month: "short", timeZone: "UTC" });
  const day = d.getUTCDate();
  if (!includeYear) return `${month} ${day}`;
  return `${month} ${day}, ${d.getUTCFullYear()}`;
}

export function StatStripModule({ module }: ModuleProps) {
  const data = (module.data ?? {}) as StatStripData;
  const stats = data.stats ?? [];
  const period = data.period ?? {};

  if (stats.length === 0) return null;

  // Period display: "Apr 26 – May 2, 2026" when both dates are set.
  let periodLabel = "";
  if (period.from && period.to) {
    periodLabel = `${formatDate(period.from)} – ${formatDate(period.to, true)}`;
  } else if (period.to) {
    periodLabel = formatDate(period.to, true);
  } else if (period.from) {
    periodLabel = formatDate(period.from, true);
  }

  return (
    <section
      className="mt-4 mb-6"
      id={`module-${module.anchor}`}
      data-testid="module-stat_strip"
      data-toc-skip
    >
      <ul
        className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 list-none"
        data-testid="stat-strip-list"
      >
        {stats.map((stat, idx) => {
          const value = (stat.value || "").trim();
          const label = (stat.label || "").trim();
          if (!value) return null;
          return (
            <li
              key={`${value}-${label}-${idx}`}
              className="rounded-lg border border-border/60 bg-muted/10 px-4 py-3 flex flex-col items-start"
              data-testid="stat-strip-card"
            >
              <span
                data-testid="stat-strip-value"
                className="text-2xl font-semibold text-foreground tracking-tight tabular-nums"
              >
                {value}
              </span>
              {label && (
                <span
                  data-testid="stat-strip-label"
                  className="mt-1 text-[11px] uppercase tracking-wider text-muted-foreground"
                >
                  {label}
                </span>
              )}
            </li>
          );
        })}
      </ul>
      {periodLabel && (
        <p
          className="mt-2 text-xs text-muted-foreground"
          data-testid="stat-strip-period"
        >
          {periodLabel}
        </p>
      )}
    </section>
  );
}
