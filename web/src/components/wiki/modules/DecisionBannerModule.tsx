/** Decision Banner module — spotlights the single decision on a
 *  Decision-archetype topic page.
 *
 *  Shape (set by `wiki/modules/decision_banner.py::build_decision_banner_data`):
 *    - `decision`: capitalized first sentence (the headline)
 *    - `body`: optional remaining prose from the source fact
 *    - `decided_by`: { name, fact_id }
 *    - `decided_at`: ISO date (YYYY-MM-DD) — empty hides the chip
 *    - `rationale`: null today; Phase 3 will populate
 *    - `alternatives_rejected`: [] today; Phase 3 will populate
 *    - `consequences_open`: [] today; Phase 3 will populate
 *    - `fact_id`: stable cite-id for LLM agents
 *    - `source_url`: optional permalink to the original message
 *
 *  Visual: indigo-tinted banner with ✅ DECIDED ribbon, 4px indigo
 *  left-border accent, headline in 18px semibold, optional body
 *  paragraph below. Empty/null fields hide their rows entirely
 *  (no "Rationale: —" placeholder text). Phase 3 will start
 *  populating rationale / alternatives / consequences without
 *  changing the contract.
 */

import type { ModuleProps } from "./ModuleRenderer";

interface DecidedBy {
  name?: string;
  fact_id?: string;
}

interface DecisionBannerData {
  label?: string;
  renderer_kind?: string;
  decision?: string;
  body?: string;
  decided_by?: DecidedBy;
  decided_at?: string;
  rationale?: string | null;
  alternatives_rejected?: string[];
  consequences_open?: string[];
  fact_id?: string;
  source_url?: string;
}

/** Format an ISO date (YYYY-MM-DD) as "Mon DD, YYYY" for the chip
 *  row. Returns the raw string when parsing fails so a malformed
 *  date renders something rather than nothing. */
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

export function DecisionBannerModule({ module }: ModuleProps) {
  const data = (module.data ?? {}) as DecisionBannerData;
  const decision = (typeof data.decision === "string" ? data.decision : "").trim();
  const body = (typeof data.body === "string" ? data.body : "").trim();
  const decidedBy = data.decided_by || {};
  const decidedByName = (typeof decidedBy.name === "string" ? decidedBy.name : "").trim();
  const decidedAt = formatDate(
    (typeof data.decided_at === "string" ? data.decided_at : "").trim(),
  );
  const rationale =
    typeof data.rationale === "string" ? data.rationale.trim() : "";
  const alternatives = Array.isArray(data.alternatives_rejected)
    ? data.alternatives_rejected.filter(
        (s) => typeof s === "string" && s.trim().length > 0,
      )
    : [];
  const consequences = Array.isArray(data.consequences_open)
    ? data.consequences_open.filter(
        (s) => typeof s === "string" && s.trim().length > 0,
      )
    : [];
  const factId = (typeof data.fact_id === "string" ? data.fact_id : "").trim();
  const sourceUrl = (typeof data.source_url === "string" ? data.source_url : "").trim();

  // Defensive — if the planner picked the module but the cluster has
  // no decision-typed fact, bail rather than render an empty banner.
  if (!decision) {
    return null;
  }

  return (
    <section
      className="mt-2 mb-6 rounded-lg border border-indigo-500/30 bg-indigo-500/5 border-l-4 border-l-indigo-500 overflow-hidden"
      id={`module-${module.anchor}`}
      data-testid="module-decision_banner"
      data-toc-skip
    >
      {/* Ribbon row — ✅ DECIDED + date + author chips */}
      <div
        data-testid="decision-banner-ribbon"
        className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2 border-b border-indigo-500/20 bg-indigo-500/10 text-xs uppercase tracking-wide font-semibold text-indigo-700 dark:text-indigo-300"
      >
        <span aria-hidden="true">✅</span>
        <span>Decided</span>
        {decidedAt && (
          <>
            <span className="text-indigo-500/60" aria-hidden="true">·</span>
            <span data-testid="decision-banner-date" className="font-normal normal-case tracking-normal">
              {decidedAt}
            </span>
          </>
        )}
        {decidedByName && (
          <>
            <span className="text-indigo-500/60" aria-hidden="true">·</span>
            <span data-testid="decision-banner-author" className="font-normal normal-case tracking-normal">
              {decidedByName}
            </span>
          </>
        )}
      </div>

      {/* Headline + body */}
      <div className="px-4 py-4">
        <p
          data-testid="decision-banner-decision"
          className="text-lg font-semibold leading-snug text-foreground"
        >
          {decision}
        </p>
        {body && (
          <p
            data-testid="decision-banner-body"
            className="mt-2 text-sm text-muted-foreground leading-relaxed"
          >
            {body}
          </p>
        )}

        {/* Rationale — Phase 3 will populate. Hidden today (always null). */}
        {rationale && (
          <p
            data-testid="decision-banner-rationale"
            className="mt-3 text-sm text-foreground/85"
          >
            <span className="font-semibold">Because:</span> {rationale}
          </p>
        )}

        {/* Alternatives rejected — hidden when empty (Phase 3 will populate) */}
        {alternatives.length > 0 && (
          <div
            data-testid="decision-banner-alternatives"
            className="mt-3"
          >
            <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-1">
              Alternatives rejected
            </p>
            <ul className="space-y-0.5 text-sm text-foreground/85">
              {alternatives.map((alt, idx) => (
                <li key={`alt-${idx}`} className="flex items-baseline gap-2">
                  <span className="text-rose-500/80" aria-hidden="true">✗</span>
                  <span>{alt}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Open consequences — hidden when empty */}
        {consequences.length > 0 && (
          <div
            data-testid="decision-banner-consequences"
            className="mt-3"
          >
            <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-1">
              Open consequences
            </p>
            <ul className="space-y-0.5 text-sm text-foreground/85">
              {consequences.map((q, idx) => (
                <li key={`csq-${idx}`} className="flex items-baseline gap-2">
                  <span className="text-amber-500/80" aria-hidden="true">❓</span>
                  <span>{q}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Footer — cite-id chip + optional source link */}
        {(factId || sourceUrl) && (
          <div
            data-testid="decision-banner-footer"
            className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground"
          >
            {factId && (
              <span
                data-testid="decision-banner-fact-id"
                className="inline-flex items-center gap-1 font-mono text-[11px]"
              >
                <span aria-hidden="true">📎</span>
                <span>Cite as: {factId}</span>
              </span>
            )}
            {sourceUrl && (
              <a
                href={sourceUrl}
                target="_blank"
                rel="noopener noreferrer"
                data-testid="decision-banner-source-link"
                className="inline-flex items-center gap-1 hover:text-foreground transition-colors"
              >
                <span aria-hidden="true">↗</span>
                <span>source</span>
              </a>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
