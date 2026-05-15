import { useState, useRef, useEffect, useMemo } from "react";
import { BookOpen, ChevronDown, Check, Library } from "lucide-react";
import { WikiStateIcon } from "@/components/shared/WikiStateIcon";
import type { WikiState } from "@/hooks/useWikiStates";
import { compareChannelsByWikiState, summarizeWikiCoverage } from "@/lib/wikiState";
import { cn } from "@/lib/utils";

export interface ChannelOption {
  channel_id: string;
  name: string;
  platform: string;
}

/** Sentinel value used when the user picks the "All wiki channels" row.
 *  Consumers should special-case this to route the question across every
 *  ready-wiki channel rather than scoping to a single one. */
export const ALL_WIKIS_VALUE = "__all_wikis__";

interface ChannelPickerProps {
  channels: ChannelOption[];
  value: string;
  onChange: (channelId: string) => void;
  disabled?: boolean;
  /** Resolves wiki state per channel for the icon + tier sort. Optional —
   *  when omitted the picker renders every row as "ready" so the dropdown
   *  is unchanged from its prior behaviour. */
  getWikiState?: (channelId: string) => WikiState;
  /** Feature-gate the "All wiki channels" workspace-wide row. The backend
   *  doesn't yet support multi-channel ask, so consumers must opt-in once
   *  they've wired the routing. */
  enableAllWikis?: boolean;
}

/**
 * Inline channel picker for the chat input. Visually confident — the
 * selected channel reads as a first-class control, not decoration.
 */
export function ChannelPicker({
  channels,
  value,
  onChange,
  disabled,
  getWikiState,
  enableAllWikis = false,
}: ChannelPickerProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const containerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) {
      setQuery("");
      return;
    }
    const t = setTimeout(() => inputRef.current?.focus(), 0);
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => {
      clearTimeout(t);
      document.removeEventListener("mousedown", handler);
    };
  }, [open]);

  const selected = channels.find((c) => c.channel_id === value);
  const isAllWikisSelected = value === ALL_WIKIS_VALUE;

  // Resolver shim — falls back to "ready" when caller didn't pass one, so
  // unaware callers still get the legacy unchanged behaviour.
  const resolveState = useMemo<(id: string) => WikiState>(
    () => getWikiState ?? (() => "ready"),
    [getWikiState],
  );

  const filteredAll = useMemo(() => {
    const q = query.trim().toLowerCase();
    const matched = q
      ? channels.filter((c) => c.name.toLowerCase().includes(q))
      : channels;
    // Sort: wiki-ready first, then empty, alphabetical within each tier.
    return [...matched].sort((a, b) =>
      compareChannelsByWikiState(a, b, resolveState),
    );
  }, [channels, query, resolveState]);

  // Split for the divider — only meaningful when wiki state is resolved.
  const { readyRows, emptyRows } = useMemo(() => {
    const ready: ChannelOption[] = [];
    const empty: ChannelOption[] = [];
    for (const ch of filteredAll) {
      const s = resolveState(ch.channel_id);
      if (s === "ready" || s === "building") ready.push(ch);
      else empty.push(ch);
    }
    return { readyRows: ready, emptyRows: empty };
  }, [filteredAll, resolveState]);

  const coverage = useMemo(
    () => summarizeWikiCoverage(channels, resolveState),
    [channels, resolveState],
  );

  // Selected-button label / icon — handle the All-wikis sentinel too so
  // the pill reads cleanly when the user has picked the workspace-wide
  // scope.
  const buttonLabel = isAllWikisSelected
    ? `All wikis (${coverage.ready})`
    : selected?.name ?? "Choose channel";
  const buttonIcon = isAllWikisSelected ? (
    <Library className="w-3.5 h-3.5 shrink-0 opacity-80" strokeWidth={2.5} />
  ) : selected ? (
    <WikiStateIcon state={resolveState(selected.channel_id)} size={14} />
  ) : (
    <BookOpen className="w-3.5 h-3.5 shrink-0 opacity-60" strokeWidth={2.5} />
  );

  const renderRow = (ch: ChannelOption) => {
    const state = resolveState(ch.channel_id);
    const isEmpty = state === "empty" || state === "errored";
    const isSelected = ch.channel_id === value;
    return (
      <button
        key={ch.channel_id}
        type="button"
        onClick={() => {
          onChange(ch.channel_id);
          setOpen(false);
        }}
        className={cn(
          "w-full flex items-center gap-2.5 px-3 py-2 text-left text-[13px] transition-colors",
          isSelected
            ? "bg-primary/8 text-primary"
            : "text-foreground/90 hover:bg-muted/60",
          isEmpty && !isSelected && "opacity-60",
        )}
        title={isEmpty ? "No wiki yet — pick this channel to ingest it" : undefined}
      >
        <WikiStateIcon state={state} size={14} />
        <span className="truncate flex-1">{ch.name}</span>
        <span className="font-mono text-[9px] uppercase tracking-widest text-muted-foreground/50 shrink-0">
          {ch.platform}
        </span>
        {isSelected && <Check className="w-3.5 h-3.5 text-primary shrink-0" />}
      </button>
    );
  };

  return (
    <div className="relative" ref={containerRef}>
      <button
        type="button"
        onClick={() => !disabled && setOpen((v) => !v)}
        disabled={disabled}
        className={`group inline-flex items-center gap-1.5 h-8 pl-2 pr-1.5 rounded-lg text-[13px] font-medium transition-all duration-150 disabled:opacity-50 ${
          selected || isAllWikisSelected
            ? "bg-primary/8 text-primary border border-primary/25 hover:bg-primary/12 hover:border-primary/40"
            : "bg-muted/50 text-muted-foreground border border-border hover:bg-muted"
        }`}
        title={
          isAllWikisSelected
            ? `Asking across ${coverage.ready} wiki channels`
            : selected
              ? `Asking in #${selected.name}`
              : "Choose a channel"
        }
      >
        {buttonIcon}
        <span className="max-w-[140px] truncate">{buttonLabel}</span>
        <ChevronDown
          className={`w-3 h-3 shrink-0 opacity-60 transition-transform duration-150 ${
            open ? "rotate-180" : ""
          }`}
        />
      </button>

      {open && (
        <div className="absolute bottom-full left-0 mb-2 w-80 bg-popover border border-border rounded-xl shadow-xl z-50 overflow-hidden motion-safe:animate-scale-in origin-bottom-left">
          {/* Search */}
          <div className="px-3 py-2 border-b border-border/60">
            <input
              ref={inputRef}
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Filter channels…"
              className="w-full text-[13px] bg-transparent text-foreground placeholder:text-muted-foreground/50 outline-none"
            />
          </div>

          {/* Coverage summary — quietly sets expectations before scroll. */}
          {getWikiState && channels.length > 0 && (
            <div className="px-3 py-1.5 border-b border-border/40 text-[11px] text-muted-foreground/70 tabular-nums">
              <span className="text-primary font-medium">{coverage.ready}</span> ready
              {coverage.empty > 0 && (
                <>
                  {" · "}
                  <span className="text-muted-foreground/60">{coverage.empty}</span> empty
                </>
              )}
            </div>
          )}

          {/* List */}
          <div className="max-h-64 overflow-y-auto py-1">
            {filteredAll.length === 0 ? (
              <div className="px-3 py-6 text-center text-xs text-muted-foreground/60">
                No channels match "{query}"
              </div>
            ) : (
              <>
                {/* All wikis (workspace-wide) — opt-in via enableAllWikis,
                    visible only when wiki state is resolved and more than
                    one channel is ready. Backend support isn't there yet,
                    so consumers must explicitly enable. */}
                {enableAllWikis && getWikiState && coverage.ready > 1 && !query.trim() && (
                  <button
                    type="button"
                    onClick={() => {
                      onChange(ALL_WIKIS_VALUE);
                      setOpen(false);
                    }}
                    className={cn(
                      "w-full flex items-center gap-2.5 px-3 py-2 text-left text-[13px] transition-colors",
                      isAllWikisSelected
                        ? "bg-primary/8 text-primary"
                        : "text-foreground/90 hover:bg-muted/60",
                    )}
                  >
                    <Library className="w-3.5 h-3.5 shrink-0 text-primary" />
                    <span className="truncate flex-1 font-medium">
                      All wiki channels
                    </span>
                    <span className="text-[10px] tabular-nums text-muted-foreground/70 shrink-0">
                      {coverage.ready}
                    </span>
                    {isAllWikisSelected && (
                      <Check className="w-3.5 h-3.5 text-primary shrink-0" />
                    )}
                  </button>
                )}

                {readyRows.map(renderRow)}

                {readyRows.length > 0 && emptyRows.length > 0 && (
                  <div className="my-1 mx-3 border-t border-border/40" />
                )}

                {emptyRows.map(renderRow)}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
