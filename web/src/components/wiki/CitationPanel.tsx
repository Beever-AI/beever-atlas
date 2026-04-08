import { useEffect, useState } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import type { WikiCitation } from "@/lib/types";
import { MediaBadge } from "./MediaBadge";

interface CitationPanelProps {
  citations: WikiCitation[];
}

const DEFAULT_VISIBLE_CITATIONS = 3;

export function CitationPanel({ citations }: CitationPanelProps) {
  const [isExpanded, setIsExpanded] = useState(false);
  const safeCitations = citations ?? [];
  const shouldCollapse = safeCitations.length > DEFAULT_VISIBLE_CITATIONS;
  const visibleCitations =
    shouldCollapse && !isExpanded
      ? safeCitations.slice(0, DEFAULT_VISIBLE_CITATIONS)
      : safeCitations;
  const hiddenCount = safeCitations.length - DEFAULT_VISIBLE_CITATIONS;

  useEffect(() => {
    const focusCitation = (citationIndex: number) => {
      if (citationIndex <= DEFAULT_VISIBLE_CITATIONS) {
        const el = document.getElementById(`citation-${citationIndex}`);
        if (!el) return;
        el.scrollIntoView({ behavior: "smooth", block: "center" });
        el.classList.add("ring-2", "ring-primary", "ring-offset-2", "ring-offset-background");
        setTimeout(
          () => el.classList.remove("ring-2", "ring-primary", "ring-offset-2", "ring-offset-background"),
          2000,
        );
        return;
      }

      setIsExpanded(true);
      requestAnimationFrame(() => {
        const el = document.getElementById(`citation-${citationIndex}`);
        if (!el) return;
        el.scrollIntoView({ behavior: "smooth", block: "center" });
        el.classList.add("ring-2", "ring-primary", "ring-offset-2", "ring-offset-background");
        setTimeout(
          () => el.classList.remove("ring-2", "ring-primary", "ring-offset-2", "ring-offset-background"),
          2000,
        );
      });
    };

    const onCitationJump = (event: Event) => {
      const customEvent = event as CustomEvent<{ index?: number }>;
      const citationIndex = customEvent.detail?.index;
      if (!citationIndex || Number.isNaN(citationIndex)) return;
      focusCitation(citationIndex);
    };

    window.addEventListener("wiki:citation-jump", onCitationJump);
    return () => window.removeEventListener("wiki:citation-jump", onCitationJump);
  }, []);

  if (safeCitations.length === 0) return null;

  return (
    <div className="mt-8 border-t border-border pt-6">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold text-muted-foreground">
          Sources <span className="text-muted-foreground/70">({safeCitations.length})</span>
        </h3>
        {shouldCollapse && (
          <button
            type="button"
            onClick={() => setIsExpanded((prev) => !prev)}
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-2.5 py-1 text-xs font-medium text-muted-foreground hover:bg-muted/40 hover:text-foreground transition-colors"
            aria-expanded={isExpanded}
            aria-controls="wiki-citations-list"
          >
            {isExpanded ? (
              <>
                <ChevronUp size={12} />
                Collapse
              </>
            ) : (
              <>
                <ChevronDown size={12} />
                Show {hiddenCount} more
              </>
            )}
          </button>
        )}
      </div>
      <div id="wiki-citations-list" className="space-y-2.5">
        {visibleCitations.map((citation, i) => (
          <div
            key={citation.id}
            id={`citation-${i + 1}`}
            className="flex gap-3 text-sm rounded-lg border border-border/60 bg-card/40 px-3 py-2.5 transition-colors hover:bg-muted/30"
          >
            <span className="mt-0.5 text-primary font-semibold font-mono w-7 shrink-0">[{i + 1}]</span>
            <div className="flex-1 min-w-0">
              <div className="flex items-start gap-2 flex-wrap">
                {citation.author && (
                  <span className="font-medium text-foreground">@{citation.author}</span>
                )}
                {citation.timestamp && (
                  <span className="text-muted-foreground/70 text-xs">{citation.timestamp}</span>
                )}
                {citation.media_type && (
                  <MediaBadge type={citation.media_type} name={citation.media_name} />
                )}
              </div>
              <p className="text-muted-foreground text-xs mt-1 leading-relaxed line-clamp-2">
                {citation.text_excerpt}
              </p>
              {citation.permalink && citation.permalink.startsWith("http") && (
                <a
                  href={citation.permalink}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-xs text-primary/80 hover:text-primary mt-1 inline-flex items-center gap-1"
                >
                  View original message ↗
                </a>
              )}
            </div>
          </div>
        ))}
      </div>
      {shouldCollapse && isExpanded && (
        <div className="mt-3">
          <button
            type="button"
            onClick={() => setIsExpanded(false)}
            className="inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
          >
            <ChevronUp size={12} />
            Collapse sources
          </button>
        </div>
      )}
    </div>
  );
}
