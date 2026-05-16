import { useEffect, useMemo, useRef, useState } from "react";
import { Search } from "lucide-react";
import type { GraphEntity } from "@/hooks/useGraph";
import { cn } from "@/lib/utils";
import { getTypeColors } from "./GraphFilters";

interface EntitySearchPaletteProps {
  entities: GraphEntity[];
  open: boolean;
  onClose: () => void;
  /** Fires with the selected entity ID; the parent forwards to the canvas
   *  ref's imperative `focusNode`. */
  onSelect: (id: string) => void;
}

const MAX_RESULTS = 25;

/**
 * cmd-K (or ctrl-K) search palette for entities. Document-level hotkey
 * binding lives in `MemoryGraphView`; this component owns the modal,
 * input focus, keyboard nav (up/down/Enter), and ESC-to-close.
 *
 * Filter: case-insensitive substring on entity name and aliases. Simple
 * but enough for v1 (per plan §E.4). No external fuzzy library.
 */
export function EntitySearchPalette({
  entities,
  open,
  onClose,
  onSelect,
}: EntitySearchPaletteProps) {
  const [query, setQuery] = useState("");
  const [activeIdx, setActiveIdx] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);

  // Reset state on open
  useEffect(() => {
    if (open) {
      setQuery("");
      setActiveIdx(0);
      // Autofocus after the modal mounts.
      const t = window.setTimeout(() => inputRef.current?.focus(), 0);
      return () => window.clearTimeout(t);
    }
  }, [open]);

  const results = useMemo(() => {
    if (!open) return [];
    const q = query.trim().toLowerCase();
    if (!q) {
      // Empty query → show the first N by name asc, so the palette is
      // useful even on cmd-K with no typing.
      return [...entities]
        .sort((a, b) => a.name.localeCompare(b.name))
        .slice(0, MAX_RESULTS);
    }
    const filtered = entities.filter((e) => {
      if (e.name.toLowerCase().includes(q)) return true;
      const aliases = e.aliases ?? [];
      return aliases.some((a) => a.toLowerCase().includes(q));
    });
    // Rank: prefix matches first, then substring matches.
    filtered.sort((a, b) => {
      const aPrefix = a.name.toLowerCase().startsWith(q) ? 0 : 1;
      const bPrefix = b.name.toLowerCase().startsWith(q) ? 0 : 1;
      if (aPrefix !== bPrefix) return aPrefix - bPrefix;
      return a.name.localeCompare(b.name);
    });
    return filtered.slice(0, MAX_RESULTS);
  }, [entities, query, open]);

  // Clamp the active index when results change.
  useEffect(() => {
    setActiveIdx((i) => {
      if (results.length === 0) return 0;
      if (i >= results.length) return results.length - 1;
      return i;
    });
  }, [results]);

  // Keep the active row in view on arrow-nav.
  useEffect(() => {
    const list = listRef.current;
    if (!list) return;
    const child = list.children.item(activeIdx) as HTMLElement | null;
    child?.scrollIntoView({ block: "nearest" });
  }, [activeIdx]);

  if (!open) return null;

  const handleKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIdx((i) => Math.min(i + 1, Math.max(0, results.length - 1)));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIdx((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const chosen = results[activeIdx];
      if (chosen) {
        onSelect(chosen.id);
        onClose();
      }
    } else if (e.key === "Escape") {
      // Stop propagation so the EntityPanel's document-level ESC handler
      // (in EntityPanel.tsx) doesn't also fire and close the panel.
      e.preventDefault();
      e.stopPropagation();
      onClose();
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center pt-[15vh] px-4"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label="Search entities"
    >
      {/* Backdrop */}
      <div className="absolute inset-0 bg-background/70 backdrop-blur-sm" />

      {/* Modal */}
      <div
        className="relative w-full max-w-lg rounded-xl border border-border bg-card shadow-2xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 px-3 py-2.5 border-b border-border">
          <Search className="w-4 h-4 text-muted-foreground shrink-0" />
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={handleKey}
            placeholder="Search entities..."
            className="flex-1 bg-transparent text-sm text-foreground placeholder:text-muted-foreground focus:outline-none"
            aria-label="Entity search query"
          />
          <kbd className="hidden sm:inline-flex items-center px-1.5 py-0.5 rounded border border-border/70 text-[10px] font-mono text-muted-foreground">
            esc
          </kbd>
        </div>

        {results.length === 0 ? (
          <div className="px-4 py-6 text-center">
            <p className="text-xs text-muted-foreground">
              {query.trim() ? "No entities match your search." : "No entities loaded."}
            </p>
          </div>
        ) : (
          <ul
            ref={listRef}
            className="max-h-[50vh] overflow-y-auto py-1"
            role="listbox"
            aria-label="Search results"
          >
            {results.map((e, idx) => {
              const colors = getTypeColors(e.type);
              const isActive = idx === activeIdx;
              return (
                <li
                  key={e.id}
                  role="option"
                  aria-selected={isActive}
                  onMouseEnter={() => setActiveIdx(idx)}
                  onClick={() => {
                    onSelect(e.id);
                    onClose();
                  }}
                  className={cn(
                    "flex items-center gap-2 px-3 py-2 cursor-pointer transition-colors",
                    isActive ? "bg-muted" : "hover:bg-muted/60",
                  )}
                >
                  <span
                    className="w-2 h-2 rounded-full shrink-0"
                    style={{ backgroundColor: colors.node }}
                  />
                  <span className="text-sm text-foreground truncate flex-1 min-w-0">
                    {e.name}
                  </span>
                  <span className="text-[10px] text-muted-foreground shrink-0">
                    {e.type}
                  </span>
                </li>
              );
            })}
          </ul>
        )}

        <div className="flex items-center gap-3 px-3 py-1.5 border-t border-border bg-muted/30 text-[10px] text-muted-foreground">
          <span className="inline-flex items-center gap-1">
            <kbd className="px-1 py-0.5 rounded border border-border/70 font-mono">↑↓</kbd>
            navigate
          </span>
          <span className="inline-flex items-center gap-1">
            <kbd className="px-1 py-0.5 rounded border border-border/70 font-mono">↵</kbd>
            select
          </span>
        </div>
      </div>
    </div>
  );
}
