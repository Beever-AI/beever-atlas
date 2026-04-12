import { useState, useRef, useEffect } from "react";
import { Hash, ChevronDown, Check } from "lucide-react";

export interface ChannelOption {
  channel_id: string;
  name: string;
  platform: string;
}

interface ChannelPickerProps {
  channels: ChannelOption[];
  value: string;
  onChange: (channelId: string) => void;
  disabled?: boolean;
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

  const filtered = query.trim()
    ? channels.filter((c) =>
        c.name.toLowerCase().includes(query.trim().toLowerCase()),
      )
    : channels;

  return (
    <div className="relative" ref={containerRef}>
      <button
        type="button"
        onClick={() => !disabled && setOpen((v) => !v)}
        disabled={disabled}
        className={`group inline-flex items-center gap-1.5 h-8 pl-2 pr-1.5 rounded-lg text-[13px] font-medium transition-all duration-150 disabled:opacity-50 ${
          selected
            ? "bg-primary/8 text-primary border border-primary/25 hover:bg-primary/12 hover:border-primary/40"
            : "bg-muted/50 text-muted-foreground border border-border hover:bg-muted"
        }`}
        title={selected ? `Asking in #${selected.name}` : "Choose a channel"}
      >
        <Hash className="w-3.5 h-3.5 shrink-0 opacity-80" strokeWidth={2.5} />
        <span className="max-w-[140px] truncate">
          {selected ? selected.name : "Choose channel"}
        </span>
        <ChevronDown
          className={`w-3 h-3 shrink-0 opacity-60 transition-transform duration-150 ${
            open ? "rotate-180" : ""
          }`}
        />
      </button>

      {open && (
        <div className="absolute bottom-full left-0 mb-2 w-72 bg-popover border border-border rounded-xl shadow-xl z-50 overflow-hidden motion-safe:animate-scale-in origin-bottom-left">
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

          {/* List */}
          <div className="max-h-64 overflow-y-auto py-1">
            {filtered.length === 0 ? (
              <div className="px-3 py-6 text-center text-xs text-muted-foreground/60">
                No channels match "{query}"
              </div>
            ) : (
              filtered.map((ch) => {
                const isSelected = ch.channel_id === value;
                return (
                  <button
                    key={ch.channel_id}
                    type="button"
                    onClick={() => {
                      onChange(ch.channel_id);
                      setOpen(false);
                    }}
                    className={`w-full flex items-center gap-2.5 px-3 py-2 text-left text-[13px] transition-colors ${
                      isSelected
                        ? "bg-primary/8 text-primary"
                        : "text-foreground/90 hover:bg-muted/60"
                    }`}
                  >
                    <Hash
                      className={`w-3.5 h-3.5 shrink-0 ${
                        isSelected ? "opacity-90" : "opacity-40"
                      }`}
                      strokeWidth={2.5}
                    />
                    <span className="truncate flex-1">{ch.name}</span>
                    <span className="font-mono text-[9px] uppercase tracking-widest text-muted-foreground/50 shrink-0">
                      {ch.platform}
                    </span>
                    {isSelected && (
                      <Check className="w-3.5 h-3.5 text-primary shrink-0" />
                    )}
                  </button>
                );
              })
            )}
          </div>
        </div>
      )}
    </div>
  );
}
