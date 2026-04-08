import { RefreshCw } from "lucide-react";

interface FreshnessBadgeProps {
  isStale: boolean;
  generatedAt: string;
  onRefresh: () => void;
  isRefreshing: boolean;
  showStatus?: boolean;
  showRefreshButton?: boolean;
  className?: string;
}

function getTimeAgo(dateStr: string): string {
  const date = new Date(dateStr);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDays = Math.floor(diffHr / 24);
  return `${diffDays}d ago`;
}

export function FreshnessBadge({
  isStale,
  generatedAt,
  onRefresh,
  isRefreshing,
  showStatus = true,
  showRefreshButton = true,
  className = "",
}: FreshnessBadgeProps) {
  const timeAgo = getTimeAgo(generatedAt);

  return (
    <div className={className}>
      {showStatus && (
        <div className="flex items-center gap-2">
          {isStale ? (
            <span className="inline-flex items-center gap-1 rounded-full bg-amber-500/15 px-2 py-0.5 text-xs font-medium text-amber-500">
              <span className="h-1.5 w-1.5 rounded-full bg-amber-500" />
              Stale
            </span>
          ) : (
            <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/15 px-2 py-0.5 text-xs font-medium text-emerald-500">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
              {timeAgo}
            </span>
          )}
        </div>
      )}
      {showRefreshButton && (
        <button
          onClick={onRefresh}
          disabled={isRefreshing}
          className={`flex items-center justify-center gap-1.5 w-full rounded-md px-3 py-1.5 text-xs font-medium transition-colors disabled:opacity-50 ${
            isStale
              ? "bg-amber-500/10 text-amber-600 hover:bg-amber-500/20 border border-amber-500/20"
              : "bg-muted/50 text-muted-foreground hover:bg-muted hover:text-foreground border border-border/50"
          }`}
          title={isRefreshing ? "Regenerating wiki..." : "Regenerate wiki"}
        >
          <RefreshCw className={`h-3 w-3 ${isRefreshing ? "animate-spin" : ""}`} />
          {isRefreshing ? "Regenerating…" : isStale ? "Regenerate Wiki" : "Regenerate"}
        </button>
      )}
    </div>
  );
}
