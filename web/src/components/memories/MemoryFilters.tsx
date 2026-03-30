import type { MemoryFilters as Filters } from "@/hooks/useMemories";
import { Search } from "lucide-react";

interface MemoryFiltersProps {
  filters: Filters;
  setFilters: (filters: Filters) => void;
}

export function MemoryFilters({ filters, setFilters }: MemoryFiltersProps) {
  function update(key: keyof Filters, value: string) {
    setFilters({ ...filters, [key]: value });
  }

  const inputClass =
    "h-10 px-3 text-sm rounded-xl border border-border bg-background text-foreground focus:outline-none focus:ring-2 focus:ring-primary/25 focus:border-primary/40 w-full";

  return (
    <div className="rounded-2xl border border-border bg-card p-4 shadow-sm">
      <div className="grid grid-cols-1 md:grid-cols-[1fr_1fr_170px_auto] gap-3 items-end">
        <div className="flex flex-col gap-1.5">
          <label className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Topic</label>
          <div className="relative">
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
            <input
              type="text"
              value={filters.topic}
              onChange={(e) => update("topic", e.target.value)}
              placeholder="Filter by topic..."
              className={`${inputClass} pl-9`}
            />
          </div>
        </div>

        <div className="flex flex-col gap-1.5">
          <label className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Entity</label>
          <div className="relative">
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
            <input
              type="text"
              value={filters.entity}
              onChange={(e) => update("entity", e.target.value)}
              placeholder="Search entities..."
              className={`${inputClass} pl-9`}
            />
          </div>
        </div>

        <div className="flex flex-col gap-1.5">
          <label className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Min Importance
          </label>
          <select
            value={filters.minImportance}
            onChange={(e) => update("minImportance", e.target.value)}
            className={inputClass}
          >
            <option value="">All</option>
            <option value="low">Low+</option>
            <option value="medium">Medium+</option>
            <option value="high">High+</option>
            <option value="critical">Critical</option>
          </select>
        </div>

        {(filters.topic || filters.entity || filters.minImportance) && (
          <button
            onClick={() =>
              setFilters({ topic: "", entity: "", minImportance: "", dateFrom: "", dateTo: "" })
            }
            className="h-10 px-3 text-sm text-muted-foreground hover:text-foreground border border-border rounded-xl hover:bg-muted transition-colors"
          >
            Clear
          </button>
        )}
      </div>
    </div>
  );
}
