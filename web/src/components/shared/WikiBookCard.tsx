import { useState } from "react";
import { Link } from "react-router-dom";
import { ArrowUpRight, MoreHorizontal, Trash2 } from "lucide-react";
import { useTheme } from "@/hooks/useTheme";
import { getPlatformBadgeStyle } from "@/lib/platform-badge";
import { WikiStateIcon } from "@/components/shared/WikiStateIcon";
import { formatRelativeTime, wikiStateLabel } from "@/lib/wikiState";
import { DeleteChannelDialog } from "@/components/shared/DeleteChannelDialog";
import type { WikiState } from "@/hooks/useWikiStates";
import { cn } from "@/lib/utils";
import { useSyncStatus } from "@/contexts/SyncStatusContext";

interface WikiBookCardProps {
  channelId: string;
  name: string;
  platform: string;
  state: WikiState;
  visitedAt?: string | null;
  lastSyncTs?: string | null;
  preface?: string | null;
  size?: "sm" | "md";
  to?: string;
  animationDelayMs?: number;
  /** When provided, a kebab (⋯) menu with a Delete action is rendered. */
  onDeleted?: (channelId: string) => void;
  /** Passed through to DeleteChannelDialog to show the sync-running warning. */
  channelKind?: "file" | "chat";
}

/**
 * Wiki-as-a-book card.
 *
 * The "book" feel comes from two things, not literal skeuomorphism:
 *   1. A platform-colored spine on the left (thin accent band, not a
 *      thick fake binding).
 *   2. **Stacked-book shadows** — two soft offset shadows on the right /
 *      bottom that make the card look like a book sitting on top of one
 *      or two more, casting depth instead of just floating.
 *
 * State differentiation is strong:
 *   - has wiki: solid border, vivid spine, stacked shadow visible.
 *   - no wiki : dashed border, ghosted spine, no stack (single empty
 *     book, nothing under it).
 */
export function WikiBookCard({
  channelId,
  name,
  platform,
  state,
  visitedAt,
  lastSyncTs,
  preface,
  size = "md",
  to,
  animationDelayMs,
  onDeleted,
  channelKind,
}: WikiBookCardProps) {
  const { resolvedTheme } = useTheme();
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [kebabOpen, setKebabOpen] = useState(false);
  const { syncingChannels } = useSyncStatus();
  const isChannelSyncing = syncingChannels.has(channelId);
  const isDark = resolvedTheme === "dark";
  const platformStyle = getPlatformBadgeStyle(platform, isDark);
  const isEmpty = state === "empty" || state === "errored";
  const href = to ?? `/channels/${channelId}/wiki`;

  // Preface — concrete signal, never the state label.
  const prefaceText = (() => {
    if (preface) return preface;
    if (visitedAt) return `Visited ${formatRelativeTime(visitedAt)}`;
    if (lastSyncTs) return `Synced ${formatRelativeTime(lastSyncTs)}`;
    if (state === "building")
      return "Building wiki — partial answers possible.";
    if (isEmpty) return "Open to ingest and build this channel's wiki.";
    return `${platform.charAt(0).toUpperCase()}${platform.slice(1)} channel.`;
  })();

  const sizes = {
    md: {
      minHeight: "min-h-[170px]",
      pad: "py-5 pr-5",
      titleClass: "text-[15px]",
      prefaceClass: "text-[13px] leading-relaxed flex-1 line-clamp-3",
      metaClass: "text-[11px]",
      spineWidth: "w-1.5",
      iconSize: 18,
      spinePadding: "pl-5",
    },
    sm: {
      minHeight: "min-h-[120px]",
      pad: "py-4 pr-4",
      titleClass: "text-sm",
      prefaceClass: "text-xs leading-relaxed flex-1 line-clamp-2",
      metaClass: "text-[10px]",
      spineWidth: "w-1",
      iconSize: 14,
      spinePadding: "pl-4",
    },
  }[size];

  // Stacked-book shadow — soft offset shadows that make the card look
  // like a book on top of one or two more. Different intensities for
  // light vs dark mode so the stack is visible in both.
  const stackShadow = isEmpty
    ? "" // empty books sit alone — no stack underneath
    : isDark
      ? "shadow-[4px_4px_0_-1px_rgba(255,255,255,0.04),8px_8px_0_-2px_rgba(255,255,255,0.025)]"
      : "shadow-[4px_4px_0_-1px_rgba(15,23,42,0.06),8px_8px_0_-2px_rgba(15,23,42,0.035)]";
  const stackShadowHover = isEmpty
    ? "hover:shadow-md"
    : isDark
      ? "hover:shadow-[6px_6px_0_-1px_rgba(255,255,255,0.06),12px_12px_0_-2px_rgba(255,255,255,0.04),0_4px_24px_-8px_hsl(var(--primary)/0.4)]"
      : "hover:shadow-[6px_6px_0_-1px_rgba(15,23,42,0.1),12px_12px_0_-2px_rgba(15,23,42,0.06),0_4px_24px_-8px_hsl(var(--primary)/0.3)]";

  // Close kebab when clicking outside
  function handleLinkClick() {
    if (kebabOpen) setKebabOpen(false);
  }

  return (
    <>
    <Link
      to={href}
      state={{ channel_name: name, platform }}
      onClick={handleLinkClick}
      // Outer wrapper carries the stacked-book shadow and the hover
      // translate. Some right/bottom margin so the stack shadow has
      // room to render without being clipped.
      className={cn(
        "group block focus:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 rounded-xl",
        "transition-all duration-200 hover:-translate-y-1 hover:-translate-x-0.5 active:translate-y-0 active:translate-x-0",
        !isEmpty && "mr-2 mb-2",
        stackShadow,
        stackShadowHover,
      )}
      style={animationDelayMs != null ? { animationDelay: `${animationDelayMs}ms` } : undefined}
    >
      <div
        className={cn(
          "relative flex items-stretch overflow-hidden rounded-xl motion-safe:animate-rise-in",
          sizes.minHeight,
          isEmpty
            ? "border border-dashed border-border/70 bg-card/50"
            : "border border-border bg-card",
        )}
      >
        {/* Spine — a clean colored accent band. Subtle inner shadow on
            the right gives it a touch of depth without faking stitches. */}
        <div
          aria-hidden
          className={cn("relative shrink-0", sizes.spineWidth)}
          style={{
            backgroundColor: platformStyle.color,
            opacity: isEmpty ? 0.25 : 0.95,
            boxShadow: isEmpty
              ? "none"
              : "inset -1px 0 0 0 rgba(0,0,0,0.18)",
          }}
        />

        {/* Body */}
        <div
          className={cn(
            "flex-1 min-w-0 flex flex-col",
            sizes.spinePadding,
            sizes.pad,
          )}
        >
          {/* Title row */}
          <div className="flex items-start gap-2 mb-2">
            <WikiStateIcon state={state} size={sizes.iconSize} />
            <span
              className={cn(
                "font-semibold truncate flex-1 transition-colors leading-snug",
                sizes.titleClass,
                isEmpty
                  ? "text-foreground/55 group-hover:text-foreground/80"
                  : "text-foreground",
              )}
              title={name}
            >
              {name}
            </span>
            {onDeleted ? (
              <div className="relative shrink-0 mt-0.5">
                <button
                  type="button"
                  aria-label="Channel actions"
                  onClick={(e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    setKebabOpen((v) => !v);
                  }}
                  className="p-0.5 rounded opacity-70 group-hover:opacity-100 hover:!opacity-100 hover:bg-muted/60 transition-opacity text-muted-foreground hover:text-foreground"
                >
                  <MoreHorizontal className="w-4 h-4" />
                </button>
                {kebabOpen && (
                  <div
                    className="absolute right-0 top-6 z-10 min-w-[130px] rounded-lg border border-border bg-card shadow-lg py-1"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <button
                      type="button"
                      onClick={(e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        setKebabOpen(false);
                        setDeleteOpen(true);
                      }}
                      className="flex w-full items-center gap-2 px-3 py-1.5 text-sm text-destructive hover:bg-destructive/10 transition-colors"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                      Delete
                    </button>
                  </div>
                )}
              </div>
            ) : (
              <ArrowUpRight
                className={cn(
                  "w-4 h-4 transition-all shrink-0 mt-0.5",
                  isEmpty
                    ? "text-muted-foreground/0 group-hover:text-muted-foreground/50"
                    : "text-muted-foreground/30 group-hover:text-primary group-hover:scale-110",
                )}
                aria-hidden
              />
            )}
          </div>

          {/* Preface */}
          <p
            className={cn(
              sizes.prefaceClass,
              isEmpty ? "text-muted-foreground/50" : "text-muted-foreground",
            )}
          >
            {prefaceText}
          </p>

          {/* Colophon — platform on left, state on right with a colored dot */}
          <div
            className={cn(
              "flex items-center justify-between mt-3 tabular-nums",
              sizes.metaClass,
            )}
          >
            <span
              className={cn(
                "inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md font-medium capitalize transition-opacity",
                isEmpty && "opacity-60",
              )}
              style={{
                backgroundColor: platformStyle.backgroundColor,
                color: platformStyle.color,
              }}
            >
              {platform}
            </span>
            <span
              className={cn(
                "inline-flex items-center gap-1.5 font-medium",
                isEmpty ? "text-muted-foreground/45" : "text-foreground/70",
              )}
            >
              <span
                aria-hidden
                className={cn(
                  "w-1.5 h-1.5 rounded-full",
                  state === "ready" && "bg-primary",
                  state === "building" && "bg-amber-500 motion-safe:animate-pulse",
                  state === "errored" && "bg-red-500",
                  state === "empty" && "bg-muted-foreground/30",
                )}
              />
              {wikiStateLabel(state)}
            </span>
          </div>
        </div>
      </div>
    </Link>

    {onDeleted && (
      <DeleteChannelDialog
        open={deleteOpen}
        channelId={channelId}
        channelName={name}
        channelKind={channelKind}
        isSyncing={isChannelSyncing}
        onClose={() => setDeleteOpen(false)}
        onDeleted={(id) => {
          setDeleteOpen(false);
          onDeleted(id);
        }}
      />
    )}
    </>
  );
}
