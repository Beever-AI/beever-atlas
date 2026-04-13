import { useEffect, useRef, useState } from "react";
import { BookOpen, ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";
import type { CitationRef, Source } from "@/types/askTypes";
import { groupRefsBySource } from "@/lib/citations";
import { SourceCard } from "./SourceCard";

interface SourcesProps {
  sources: Source[];
  refs: CitationRef[];
  messageId: string;
}

const DEFAULT_VISIBLE = 3;

/**
 * Collapsible "Sources (N)" footer for an assistant turn.
 *
 * Renders one SourceCard per unique source (deduped via first-appearance
 * order in refs). Listens for `chat:citation-jump` events — fired by
 * inline `[N]` chips — and scrolls/flashes the matching card.
 */
export function Sources({ sources, refs, messageId }: SourcesProps) {
  const [expanded, setExpanded] = useState(false);
  const [showAll, setShowAll] = useState(sources.length <= DEFAULT_VISIBLE);
  const rootRef = useRef<HTMLDivElement>(null);

  const refsBySource = groupRefsBySource(refs);
  const shouldCollapseExtras = sources.length > DEFAULT_VISIBLE;
  const visibleSources = showAll ? sources : sources.slice(0, DEFAULT_VISIBLE);
  const hiddenCount = sources.length - DEFAULT_VISIBLE;

  useEffect(() => {
    const onJump = (event: Event) => {
      const custom = event as CustomEvent<{
        index?: number;
        messageId?: string;
      }>;
      const detail = custom.detail;
      if (!detail?.index || !Number.isFinite(detail.index)) return;
      if (detail.messageId && detail.messageId !== messageId) return;

      // Resolve the marker to a source index in our deduped list.
      const ref = refs.find((r) => r.marker === detail.index);
      if (!ref) return;
      const cardIndex = sources.findIndex((s) => s.id === ref.source_id);
      if (cardIndex < 0) return;
      const displayIndex = cardIndex + 1;

      setExpanded(true);
      if (shouldCollapseExtras && displayIndex > DEFAULT_VISIBLE) {
        setShowAll(true);
      }

      requestAnimationFrame(() => {
        const el = rootRef.current?.querySelector<HTMLElement>(
          `[data-citation-id="${messageId}-${displayIndex}"]`,
        );
        if (!el) return;
        el.scrollIntoView({ behavior: "smooth", block: "center" });
        el.classList.add(
          "ring-2",
          "ring-primary",
          "ring-offset-2",
          "ring-offset-background",
        );
        window.setTimeout(() => {
          el.classList.remove(
            "ring-2",
            "ring-primary",
            "ring-offset-2",
            "ring-offset-background",
          );
        }, 1200);
      });
    };
    window.addEventListener("chat:citation-jump", onJump);
    return () => window.removeEventListener("chat:citation-jump", onJump);
  }, [messageId, shouldCollapseExtras, refs, sources]);

  if (sources.length === 0) return null;

  return (
    <div ref={rootRef} className="mt-3">
      <button
        onClick={() => setExpanded((v) => !v)}
        className="inline-flex items-center gap-2 text-xs text-muted-foreground hover:text-foreground transition-colors"
      >
        <BookOpen className="size-3.5" strokeWidth={2} />
        <span>Sources ({sources.length})</span>
        <ChevronDown
          className={cn(
            "size-3 transition-transform",
            expanded ? "rotate-0" : "-rotate-90",
          )}
        />
      </button>

      <div
        className={cn(
          "overflow-hidden transition-all duration-300 ease-in-out",
          expanded ? "opacity-100 mt-2" : "max-h-0 opacity-0",
        )}
      >
        <div className="flex flex-col gap-1.5">
          {visibleSources.map((s, i) => (
            <SourceCard
              key={s.id}
              source={s}
              index={i + 1}
              refCount={refsBySource.get(s.id)?.length ?? 1}
              messageId={messageId}
            />
          ))}
        </div>

        {shouldCollapseExtras && !showAll && (
          <button
            onClick={() => setShowAll(true)}
            className="mt-2 inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
          >
            <ChevronDown className="size-3" />
            Show {hiddenCount} more
          </button>
        )}
        {shouldCollapseExtras && showAll && sources.length > DEFAULT_VISIBLE && (
          <button
            onClick={() => setShowAll(false)}
            className="mt-2 inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
          >
            <ChevronDown className="size-3 rotate-180" />
            Collapse
          </button>
        )}
      </div>
    </div>
  );
}
