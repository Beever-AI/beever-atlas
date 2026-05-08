/**
 * ActivityFeed
 *
 * Renders the last ~10 ``recent_events`` entries surfaced by
 * ``GET /api/channels/{id}/sync/status`` (PR-3 — sync-pipeline-feedback-and-auto-wiki).
 * Each row shows:
 *   • relative timestamp
 *   • per-stage emoji icon (fetch, preprocess, extract_facts, ...)
 *   • plain-English label emitted by the worker
 *
 * Designed as a thin display component — no fetching, no polling. The
 * parent passes events from ``SyncState.recent_events`` and rerenders
 * happen on the existing ``/sync/status`` poll cadence.
 */
import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";
import type { RecentEvent } from "@/lib/types";

const STAGE_ICONS: Record<string, string> = {
  fetch: "📥",
  fetched: "📥",
  preprocess: "🧹",
  extract: "💎",
  extract_facts: "💎",
  extract_entities: "🏷",
  embed: "⚡",
  validate: "✓",
  persist: "💾",
  wiki_maintenance: "📝",
  overview_wiki: "📘",
};

/** Tiny inline relative-time formatter — date-fns is NOT a dependency
 *  and we want to keep the bundle lean. The worker emits ISO strings,
 *  rounded to the nearest minute is plenty for a "last 10 events"
 *  feed. */
function relativeTime(iso: string): string {
  try {
    const delta = Date.now() - new Date(iso).getTime();
    if (!Number.isFinite(delta)) return iso;
    const abs = Math.abs(delta);
    const seconds = Math.round(abs / 1000);
    if (seconds < 5) return "just now";
    if (seconds < 60) return `${seconds}s ago`;
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.round(hours / 24);
    return `${days}d ago`;
  } catch {
    return iso;
  }
}

export interface ActivityFeedProps {
  events: RecentEvent[] | null | undefined;
  /** Caps the rendered row count. Default 10 (matches the worker's
   *  in-memory ring buffer size). */
  maxItems?: number;
  /** Copy used when ``events`` is empty. */
  emptyMessage?: string;
  /** Wraps the feed in a ``<details>`` element with a clickable
   *  summary. Useful for the SyncProgress "below the fold" placement
   *  per spec U2. */
  collapsible?: boolean;
  /** Default open state for the ``<details>`` wrapper. Ignored when
   *  ``collapsible`` is false. */
  defaultOpen?: boolean;
  /** Visible label for the collapsible summary. */
  title?: string;
}

export function ActivityFeed({
  events,
  maxItems = 10,
  emptyMessage = "No activity yet",
  collapsible = false,
  defaultOpen = true,
  title = "Activity",
}: ActivityFeedProps) {
  const [open, setOpen] = useState(defaultOpen);

  const list = (events ?? []).slice(0, maxItems);
  const body =
    list.length === 0 ? (
      <p
        data-testid="activity-feed-empty"
        className="text-xs text-muted-foreground italic"
      >
        {emptyMessage}
      </p>
    ) : (
      <ol
        data-testid="activity-feed-list"
        className="space-y-1.5 text-xs"
      >
        {list.map((e, i) => (
          <li
            key={`${e.ts}-${i}`}
            data-testid="activity-feed-row"
            className="flex items-start gap-2 leading-snug"
          >
            <span className="font-mono text-[10px] text-muted-foreground shrink-0 mt-0.5 tabular-nums">
              {relativeTime(e.ts)}
            </span>
            <span
              aria-hidden
              className="shrink-0 mt-0.5 select-none"
              title={e.stage}
            >
              {STAGE_ICONS[e.stage] ?? "·"}
            </span>
            <span className="text-foreground/80 truncate flex-1">
              {e.label}
            </span>
          </li>
        ))}
      </ol>
    );

  if (!collapsible) {
    return <div data-testid="activity-feed">{body}</div>;
  }

  return (
    <div data-testid="activity-feed">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className={cn(
          "flex w-full items-center gap-1 text-[11px] font-medium",
          "text-muted-foreground hover:text-foreground transition-colors",
        )}
      >
        {open ? (
          <ChevronDown className="h-3 w-3" aria-hidden />
        ) : (
          <ChevronRight className="h-3 w-3" aria-hidden />
        )}
        {title}
        {list.length > 0 && (
          <span className="ml-1 text-[10px] text-muted-foreground/70">
            ({list.length})
          </span>
        )}
      </button>
      {open && <div className="mt-2">{body}</div>}
    </div>
  );
}
