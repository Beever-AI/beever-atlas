import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";
import type { Source } from "@/types/askTypes";
import { CitationHoverCard } from "./CitationHoverCard";

interface CitationChipProps {
  n: number;
  messageId: string;
  source?: Source;
}

const OPEN_DELAY_MS = 150;
const CLOSE_DELAY_MS = 100;

/**
 * Small clickable `[N]` chip rendered inline in assistant prose.
 *
 * Click → dispatches `chat:citation-jump`; the Sources footer listens
 * and scrolls/flashes the matching card.
 *
 * Hover/focus → opens a rich `CitationHoverCard` with source title,
 * metadata, excerpt, and permalink. Closes on leave with a short grace
 * so the user can move the cursor onto the card.
 */
export function CitationChip({ n, messageId, source }: CitationChipProps) {
  const [hoverCardAnchor, setHoverCardAnchor] = useState<DOMRect | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const openTimerRef = useRef<number | null>(null);
  const closeTimerRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (openTimerRef.current) window.clearTimeout(openTimerRef.current);
      if (closeTimerRef.current) window.clearTimeout(closeTimerRef.current);
    };
  }, []);

  const onClick = () => {
    window.dispatchEvent(
      new CustomEvent("chat:citation-jump", {
        detail: { index: n, messageId },
      }),
    );
  };

  const openSoon = () => {
    if (!source) return;
    if (closeTimerRef.current) {
      window.clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
    if (openTimerRef.current) return;
    openTimerRef.current = window.setTimeout(() => {
      openTimerRef.current = null;
      const rect = buttonRef.current?.getBoundingClientRect();
      if (rect) setHoverCardAnchor(rect);
    }, OPEN_DELAY_MS);
  };

  const closeSoon = () => {
    if (openTimerRef.current) {
      window.clearTimeout(openTimerRef.current);
      openTimerRef.current = null;
    }
    if (closeTimerRef.current) return;
    closeTimerRef.current = window.setTimeout(() => {
      closeTimerRef.current = null;
      setHoverCardAnchor(null);
    }, CLOSE_DELAY_MS);
  };

  const cancelClose = () => {
    if (closeTimerRef.current) {
      window.clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
  };

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        onClick={onClick}
        onMouseEnter={openSoon}
        onMouseLeave={closeSoon}
        onFocus={openSoon}
        onBlur={closeSoon}
        className={cn(
          "mx-0.5 inline-flex h-4 min-w-[1.2rem] items-center justify-center",
          "rounded-md bg-primary/10 px-1 text-[10px] font-semibold text-primary",
          "align-text-top hover:bg-primary/20 transition-colors",
          "focus:outline-none focus:ring-1 focus:ring-primary/40",
        )}
        aria-label={`Citation ${n}`}
      >
        {n}
      </button>

      {hoverCardAnchor && source && (
        <CitationHoverCard
          source={source}
          anchor={hoverCardAnchor}
          onMouseEnter={cancelClose}
          onMouseLeave={closeSoon}
        />
      )}
    </>
  );
}
