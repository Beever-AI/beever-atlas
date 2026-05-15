import { cn } from "@/lib/utils";

/**
 * Deterministic color assignment for entity types.
 * Known types get brand-harmonized colors; unknown types get
 * a consistent color derived from a hash of the type name.
 */
const KNOWN_TYPE_COLORS: Record<string, { pill: string; pillActive: string; node: string; nodeBorder: string }> = {
  Person:     { pill: "bg-teal-500/10 text-teal-700 dark:text-teal-300 border-teal-500/20", pillActive: "bg-teal-600 text-white border-teal-600", node: "#0B4F6C", nodeBorder: "#083d54" },
  Technology: { pill: "bg-indigo-500/10 text-indigo-700 dark:text-indigo-300 border-indigo-500/20", pillActive: "bg-indigo-600 text-white border-indigo-600", node: "#4F46E5", nodeBorder: "#3730A3" },
  Project:    { pill: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 border-emerald-500/20", pillActive: "bg-emerald-600 text-white border-emerald-600", node: "#059669", nodeBorder: "#047857" },
  Decision:   { pill: "bg-amber-500/10 text-amber-700 dark:text-amber-300 border-amber-500/20", pillActive: "bg-amber-600 text-white border-amber-600", node: "#D97706", nodeBorder: "#B45309" },
  Team:       { pill: "bg-cyan-500/10 text-cyan-700 dark:text-cyan-300 border-cyan-500/20", pillActive: "bg-cyan-600 text-white border-cyan-600", node: "#0891B2", nodeBorder: "#0E7490" },
  Meeting:    { pill: "bg-slate-500/10 text-slate-700 dark:text-slate-300 border-slate-500/20", pillActive: "bg-slate-600 text-white border-slate-600", node: "#475569", nodeBorder: "#334155" },
  Artifact:   { pill: "bg-rose-500/10 text-rose-700 dark:text-rose-300 border-rose-500/20", pillActive: "bg-rose-600 text-white border-rose-600", node: "#E11D48", nodeBorder: "#BE123C" },
  Document:   { pill: "bg-orange-500/10 text-orange-700 dark:text-orange-300 border-orange-500/20", pillActive: "bg-orange-600 text-white border-orange-600", node: "#EA580C", nodeBorder: "#C2410C" },
  Image:      { pill: "bg-sky-500/10 text-sky-700 dark:text-sky-300 border-sky-500/20", pillActive: "bg-sky-600 text-white border-sky-600", node: "#0284C7", nodeBorder: "#0369A1" },
  Link:       { pill: "bg-green-500/10 text-green-700 dark:text-green-300 border-green-500/20", pillActive: "bg-green-600 text-white border-green-600", node: "#16A34A", nodeBorder: "#15803D" },
  Media:      { pill: "bg-violet-500/10 text-violet-700 dark:text-violet-300 border-violet-500/20", pillActive: "bg-violet-600 text-white border-violet-600", node: "#7C3AED", nodeBorder: "#6D28D9" },
  Client:     { pill: "bg-pink-500/10 text-pink-700 dark:text-pink-300 border-pink-500/20", pillActive: "bg-pink-600 text-white border-pink-600", node: "#DB2777", nodeBorder: "#BE185D" },
  Channel:    { pill: "bg-fuchsia-500/10 text-fuchsia-700 dark:text-fuchsia-300 border-fuchsia-500/20", pillActive: "bg-fuchsia-600 text-white border-fuchsia-600", node: "#C026D3", nodeBorder: "#A21CAF" },
  Location:   { pill: "bg-lime-500/10 text-lime-700 dark:text-lime-300 border-lime-500/20", pillActive: "bg-lime-600 text-white border-lime-600", node: "#65A30D", nodeBorder: "#4D7C0F" },
};

// Generate a deterministic color from a string hash for unknown types
function hashColor(str: string): { pill: string; pillActive: string; node: string; nodeBorder: string } {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    hash = str.charCodeAt(i) + ((hash << 5) - hash);
  }
  const hue = Math.abs(hash) % 360;
  return {
    pill: `bg-gray-500/10 text-gray-700 dark:text-gray-300 border-gray-500/20`,
    pillActive: `bg-gray-600 text-white border-gray-600`,
    node: `hsl(${hue}, 55%, 50%)`,
    nodeBorder: `hsl(${hue}, 55%, 38%)`,
  };
}

export function getTypeColors(type: string) {
  return KNOWN_TYPE_COLORS[type] ?? hashColor(type);
}

interface GraphFiltersProps {
  entityTypes: string[];  // derived from actual data
  selected: string[];
  onChange: (types: string[]) => void;
}

export function GraphFilters({ entityTypes, selected, onChange }: GraphFiltersProps) {
  function toggle(type: string) {
    if (selected.includes(type)) {
      onChange(selected.filter((t) => t !== type));
    } else {
      onChange([...selected, type]);
    }
  }

  return (
    // No outer border or background — the parent header strip in
    // ``MemoryGraphView`` owns the chrome. Filters sit as a quiet
    // secondary row beneath the primary view-toggle.
    <div className="flex items-center gap-2 flex-wrap">
      <span className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground/60 shrink-0">
        Show
      </span>
      <div className="flex flex-wrap gap-1.5">
        {entityTypes.map((type) => {
          const active = selected.includes(type);
          const colors = getTypeColors(type);
          return (
            <button
              key={type}
              onClick={() => toggle(type)}
              className={cn(
                "inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[11px] font-medium border transition-colors",
                active
                  ? "border-border/70 text-foreground bg-muted"
                  : "border-border/40 text-muted-foreground/70 bg-transparent hover:border-border hover:text-foreground",
              )}
            >
              <span
                className="w-1.5 h-1.5 rounded-full shrink-0"
                style={{ backgroundColor: active ? colors.node : undefined }}
              />
              {type}
            </button>
          );
        })}
      </div>
      {selected.length < entityTypes.length && (
        <button
          onClick={() => onChange([...entityTypes])}
          className="text-[11px] text-muted-foreground/70 hover:text-foreground transition-colors ml-1"
        >
          Show all
        </button>
      )}
    </div>
  );
}
