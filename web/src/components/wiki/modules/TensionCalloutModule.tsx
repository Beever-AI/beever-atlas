/** Tension Callout module — surfaces a contradicting position pair
 *  near the top of a topic page when the heuristic detector finds
 *  opposing-sentiment facts that share an entity tag.
 *
 *  Shape (set by `wiki/modules/tension_callout.py::build_tension_callout_data`):
 *    - `title`: headline summarising the disagreement (≤80 chars)
 *    - `status`: "open" | "blocked" | "deferred"
 *    - `since`: ISO date (YYYY-MM-DD) — empty hides the date chip
 *    - `positions`: list of { author, stance, fact_id }
 *    - `tension_id`: stable cite-id for LLM agents
 *
 *  Visual: amber-tinted callout with 4px amber left-border accent,
 *  ⚠ TENSION ribbon at top, status pill (open=amber, blocked=red,
 *  deferred=gray), title in 16px semibold, then a 2-column grid of
 *  position cards (collapses to stacked on mobile). Each card shows
 *  the contributor's name (bold), their stance (text), and a
 *  `→ cite fact_id` footer chip for LLM-agent provenance.
 *
 *  Empty payload (no title) returns null — defensive against the
 *  planner picking the module despite the predicate failing.
 */

import type { ModuleProps } from "./ModuleRenderer";

interface TensionPosition {
  author?: string;
  stance?: string;
  fact_id?: string;
}

interface TensionCalloutData {
  label?: string;
  renderer_kind?: string;
  title?: string;
  status?: string;
  since?: string;
  positions?: TensionPosition[];
  tension_id?: string;
}

/** Format an ISO date (YYYY-MM-DD) as "Mon DD" for the ribbon row.
 *  Returns the raw string when parsing fails so a malformed date
 *  renders something rather than nothing. */
function formatShortDate(iso: string): string {
  if (!iso) return "";
  const m = iso.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return iso;
  const [, , mm, dd] = m;
  const months = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
  ];
  const monthIdx = parseInt(mm, 10) - 1;
  if (monthIdx < 0 || monthIdx > 11) return iso;
  const day = parseInt(dd, 10);
  if (Number.isNaN(day)) return iso;
  return `${months[monthIdx]} ${day}`;
}

/** Pick status pill color classes — open is the default (amber to
 *  match the callout accent). Blocked/deferred stand out by going
 *  red/gray respectively so a reader scanning the page can tell at
 *  a glance whether the tension is still live. */
function statusPillClasses(status: string): string {
  switch (status) {
    case "blocked":
      return "bg-rose-500/15 text-rose-700 dark:text-rose-300 border-rose-500/30";
    case "deferred":
      return "bg-muted/40 text-muted-foreground border-border";
    case "open":
    default:
      return "bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/30";
  }
}

export function TensionCalloutModule({ module }: ModuleProps) {
  const data = (module.data ?? {}) as TensionCalloutData;
  const title = (typeof data.title === "string" ? data.title : "").trim();
  const rawStatus = (typeof data.status === "string" ? data.status : "open").trim().toLowerCase();
  const status = rawStatus || "open";
  const since = formatShortDate(
    (typeof data.since === "string" ? data.since : "").trim(),
  );
  const positions = Array.isArray(data.positions)
    ? data.positions.filter(
        (p) => p && typeof p === "object",
      )
    : [];
  const tensionId = (typeof data.tension_id === "string" ? data.tension_id : "").trim();

  // Defensive — if the planner picked the module but the detector
  // returned no tension, bail rather than render an empty callout.
  if (!title || positions.length === 0) {
    return null;
  }

  return (
    <section
      className="mt-2 mb-6 rounded-lg border border-amber-500/30 bg-amber-500/5 border-l-4 border-l-amber-500 overflow-hidden"
      id={`module-${module.anchor}`}
      data-testid="module-tension_callout"
      data-toc-skip
    >
      {/* Ribbon row — ⚠ TENSION + status pill + since date */}
      <div
        data-testid="tension-callout-ribbon"
        className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2 border-b border-amber-500/20 bg-amber-500/10 text-xs uppercase tracking-wide font-semibold text-amber-700 dark:text-amber-300"
      >
        <span aria-hidden="true">⚠</span>
        <span>Tension</span>
        <span
          data-testid="tension-callout-status"
          className={`inline-flex items-center rounded-full border px-2 py-[1px] text-[10px] font-semibold uppercase tracking-wide ${statusPillClasses(status)}`}
        >
          {status}
        </span>
        {since && (
          <>
            <span className="text-amber-500/60" aria-hidden="true">·</span>
            <span data-testid="tension-callout-since" className="font-normal normal-case tracking-normal">
              Since {since}
            </span>
          </>
        )}
      </div>

      {/* Body — title + 2-column position grid */}
      <div className="px-4 py-4">
        <p
          data-testid="tension-callout-title"
          className="text-base font-semibold leading-snug text-foreground"
        >
          {title}
        </p>

        <div
          data-testid="tension-callout-positions"
          className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2"
        >
          {positions.map((p, idx) => {
            const author = (typeof p.author === "string" ? p.author : "").trim();
            const stance = (typeof p.stance === "string" ? p.stance : "").trim();
            const factId = (typeof p.fact_id === "string" ? p.fact_id : "").trim();
            return (
              <div
                key={`pos-${idx}-${factId || author || "anon"}`}
                data-testid={`tension-callout-position-${idx}`}
                className="rounded-md border border-border/60 bg-background/40 px-3 py-2.5"
              >
                <p className="text-sm font-semibold text-foreground">
                  {author || "Unknown"}
                </p>
                {stance && (
                  <p
                    data-testid={`tension-callout-stance-${idx}`}
                    className="mt-1 text-sm text-muted-foreground leading-relaxed"
                  >
                    {stance}
                  </p>
                )}
                {factId && (
                  <p
                    data-testid={`tension-callout-cite-${idx}`}
                    className="mt-2 inline-flex items-center gap-1 font-mono text-[11px] text-muted-foreground"
                  >
                    <span aria-hidden="true">→</span>
                    <span>cite {factId}</span>
                  </p>
                )}
              </div>
            );
          })}
        </div>

        {/* Footer — tension cite-id chip for LLM agent provenance */}
        {tensionId && (
          <div
            data-testid="tension-callout-footer"
            className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground"
          >
            <span
              data-testid="tension-callout-tension-id"
              className="inline-flex items-center gap-1 font-mono text-[11px]"
            >
              <span aria-hidden="true">📎</span>
              <span>Cite as: {tensionId}</span>
            </span>
          </div>
        )}
      </div>
    </section>
  );
}
