/** Hero Summary module — renders the page header.
 *
 *  Shape (set by `wiki/modules/hero_summary.py::build_hero_summary_data`):
 *    - `tldr`: a single bold sentence (the key insight)
 *    - `summary`: a 2-3 sentence overview
 *    - `highlights`: { critical_count, decision_count,
 *                      open_question_count, tension_count }
 *
 *  Visual: bold TL;DR (lg + leading-snug), smaller summary prose,
 *  then a compact stat strip showing the highlight counts as small
 *  chips with icons. Stat chips render only when their count > 0
 *  so empty pages don't show a row of zeros.
 */

import type { ModuleProps } from "./ModuleRenderer";

interface Highlights {
  critical_count?: number;
  decision_count?: number;
  open_question_count?: number;
  tension_count?: number;
}

interface HeroSummaryData {
  label?: string;
  renderer_kind?: string;
  tldr?: string;
  summary?: string;
  highlights?: Highlights;
}

/** One stat chip in the strip. Skips itself when ``count`` is 0 so
 *  the strip stays terse. */
function StatChip({
  icon,
  count,
  label,
  testid,
}: {
  icon: string;
  count: number;
  label: string;
  testid: string;
}) {
  if (!count) return null;
  return (
    <span
      data-testid={testid}
      className="inline-flex items-center gap-1.5 px-2 py-1 rounded-md bg-muted/50 text-xs text-muted-foreground"
    >
      <span aria-hidden="true">{icon}</span>
      <span className="font-semibold text-foreground">{count}</span>
      <span>{label}</span>
    </span>
  );
}

/** Strip the leading/trailing markdown-bold markers (``**...**``) the
 *  planner LLM emits around the TL;DR. The frontend already styles
 *  the TL;DR as bold; the literal asterisks would otherwise render as
 *  plain text. */
function stripBoldMarkers(s: string): string {
  if (!s) return s;
  let out = s.trim();
  if (out.startsWith("**") && out.endsWith("**") && out.length >= 4) {
    out = out.slice(2, -2).trim();
  }
  return out;
}

export function HeroSummaryModule({ module }: ModuleProps) {
  const data = (module.data ?? {}) as HeroSummaryData;
  const tldr = stripBoldMarkers(typeof data.tldr === "string" ? data.tldr : "");
  const summary = (typeof data.summary === "string" ? data.summary : "").trim();
  const h = data.highlights || {};

  if (!tldr && !summary) {
    return null;
  }

  const total =
    (h.critical_count || 0) +
    (h.decision_count || 0) +
    (h.open_question_count || 0) +
    (h.tension_count || 0);

  return (
    <section
      className="mt-2 mb-6"
      id={`module-${module.anchor}`}
      data-testid="module-hero_summary"
      data-toc-skip
    >
      {tldr && (
        <p
          data-testid="hero-summary-tldr"
          className="text-lg font-semibold leading-snug text-foreground"
        >
          {tldr}
        </p>
      )}
      {summary && (
        <p
          data-testid="hero-summary-summary"
          className="mt-3 text-sm text-muted-foreground leading-relaxed"
        >
          {summary}
        </p>
      )}
      {total > 0 && (
        <div
          data-testid="hero-summary-stats"
          className="mt-4 flex flex-wrap items-center gap-2"
        >
          <StatChip
            icon="⚡"
            count={h.critical_count || 0}
            label="critical"
            testid="hero-stat-critical"
          />
          <StatChip
            icon="✅"
            count={h.decision_count || 0}
            label={(h.decision_count || 0) === 1 ? "decision" : "decisions"}
            testid="hero-stat-decision"
          />
          <StatChip
            icon="❓"
            count={h.open_question_count || 0}
            label={
              (h.open_question_count || 0) === 1 ? "question" : "questions"
            }
            testid="hero-stat-open_question"
          />
          <StatChip
            icon="⚠"
            count={h.tension_count || 0}
            label={(h.tension_count || 0) === 1 ? "tension" : "tensions"}
            testid="hero-stat-tension"
          />
        </div>
      )}
    </section>
  );
}
