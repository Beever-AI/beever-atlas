/** Contributor card — replaces flat bullet list of "(expert)" tags
 *  with a visual chip + role + one-liner. Initials avatar gives the
 *  eye an anchor; role-group framing (e.g., "Project Development &
 *  Integration") comes from the LLM. */
import type { ReactNode } from "react";

export interface ContributorEntry {
  name: string;
  role: string;
  /** One-line contribution sentence, citation markers preserved. */
  contribution: string;
  /** Numeric citation indices to render as chips alongside name. */
  citations: number[];
}

export interface ContributorGroup {
  group: string;
  entries: ContributorEntry[];
}

interface ContributorCardProps {
  entry: ContributorEntry;
  onCitationClick?: (index: number) => void;
}

function initialsOf(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0][0]?.toUpperCase() ?? "?";
  return ((parts[0][0] ?? "") + (parts[parts.length - 1][0] ?? "")).toUpperCase();
}

export function ContributorCard({ entry, onCitationClick }: ContributorCardProps) {
  const initials = initialsOf(entry.name);
  return (
    <div className="rounded-xl border border-border bg-card p-3 hover:border-primary/30 hover:shadow-sm transition-all flex gap-3">
      <div
        className="shrink-0 flex h-10 w-10 items-center justify-center rounded-full bg-primary/10 text-primary text-[12px] font-semibold tabular-nums"
        aria-hidden="true"
      >
        {initials}
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-baseline gap-1.5 flex-wrap">
          <span className="text-sm font-semibold text-foreground line-clamp-1">
            {entry.name}
          </span>
          {entry.citations.slice(0, 3).map((idx) => (
            <button
              key={`cite-${idx}`}
              type="button"
              onClick={() => onCitationClick?.(idx)}
              className="text-[10px] tabular-nums px-1 rounded bg-muted text-muted-foreground/80 hover:bg-primary/10 hover:text-primary transition-colors"
              aria-label={`Citation ${idx}`}
              title={`Citation ${idx}`}
            >
              {idx}
            </button>
          ))}
        </div>
        {entry.role && (
          <div className="text-[11px] text-muted-foreground/70 font-medium uppercase tracking-wide mt-0.5">
            {entry.role}
          </div>
        )}
        {entry.contribution && (
          <p className="text-[12px] text-muted-foreground leading-relaxed mt-1 line-clamp-3">
            {entry.contribution}
          </p>
        )}
      </div>
    </div>
  );
}

interface ContributorGridProps {
  groups: ContributorGroup[];
  onCitationClick?: (index: number) => void;
  /** Optional title override; falls back to translated "Key contributors". */
  title?: ReactNode;
}

export function ContributorGrid({ groups, onCitationClick, title }: ContributorGridProps) {
  if (groups.length === 0) return null;
  return (
    <div className="mt-8" data-toc-skip>
      {title && (
        <h2 className="text-lg font-semibold text-foreground mb-3">{title}</h2>
      )}
      {groups.map((g, gi) => (
        <div key={`g-${gi}`} className="mb-4 last:mb-0">
          {g.group && (
            <h3 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest mb-2">
              {g.group}
            </h3>
          )}
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {g.entries.map((e, ei) => (
              <ContributorCard
                key={`${gi}-${ei}-${e.name}`}
                entry={e}
                onCitationClick={onCitationClick}
              />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
