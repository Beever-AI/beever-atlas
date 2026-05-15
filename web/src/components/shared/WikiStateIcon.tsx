import { AlertTriangle, BookOpen, Hash, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import type { WikiState } from "@/hooks/useWikiStates";

interface WikiStateIconProps {
  state: WikiState;
  size?: number;
  className?: string;
}

/**
 * Single source of truth for the wiki-state icon language used in the
 * sidebar, the ask channel picker, and the home page recent-channels list.
 * Keeping it in one component means "what does a wiki-ready channel look
 * like?" has exactly one answer across the app — change here, change
 * everywhere.
 */
export function WikiStateIcon({ state, size = 13, className }: WikiStateIconProps) {
  switch (state) {
    case "ready":
      return (
        <BookOpen
          size={size}
          className={cn("shrink-0 text-primary", className)}
          strokeWidth={2}
          aria-label="Wiki ready"
        />
      );
    case "building":
      return (
        <Loader2
          size={size}
          className={cn(
            "shrink-0 text-amber-500 dark:text-amber-400 motion-safe:animate-spin",
            className,
          )}
          aria-label="Building wiki"
        />
      );
    case "errored":
      return (
        <AlertTriangle
          size={size}
          className={cn("shrink-0 text-red-500 dark:text-red-400", className)}
          aria-label="Wiki errored"
        />
      );
    case "empty":
    default:
      return (
        <Hash
          size={size}
          className={cn("shrink-0 opacity-30", className)}
          aria-label="No wiki yet"
        />
      );
  }
}
