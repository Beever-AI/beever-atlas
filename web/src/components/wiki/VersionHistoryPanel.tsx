import { History, X, Loader2, ArrowLeft } from "lucide-react";
import type { WikiVersionSummary } from "@/lib/types";

function getTimeAgo(dateStr: string): string {
  const date = new Date(dateStr);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDays = Math.floor(diffHr / 24);
  return `${diffDays}d ago`;
}

function formatDate(dateStr: string): string {
  return new Date(dateStr).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

interface VersionHistoryPanelProps {
  versions: WikiVersionSummary[];
  isLoading: boolean;
  activeVersionNumber: number | null;
  onSelectVersion: (versionNumber: number) => void;
  onBackToCurrent: () => void;
  onClose: () => void;
}

export function VersionHistoryPanel({
  versions,
  isLoading,
  activeVersionNumber,
  onSelectVersion,
  onBackToCurrent,
  onClose,
}: VersionHistoryPanelProps) {
  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between px-4 py-3 border-b border-border shrink-0">
        <div className="flex items-center gap-2">
          <History className="h-4 w-4 text-muted-foreground" />
          <h4 className="text-sm font-semibold text-foreground">Version History</h4>
        </div>
        <button
          onClick={onClose}
          className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
          aria-label="Close version history"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto min-h-0">
        {isLoading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
          </div>
        ) : versions.length === 0 ? (
          <div className="px-4 py-8 text-center text-sm text-muted-foreground">
            No previous versions yet.
          </div>
        ) : (
          <div className="py-2">
            {/* Back to current button */}
            <button
              onClick={onBackToCurrent}
              className={`flex items-center gap-2 w-full px-4 py-2.5 text-left text-sm transition-colors ${
                activeVersionNumber === null
                  ? "bg-primary/10 text-primary font-medium"
                  : "text-muted-foreground hover:bg-muted hover:text-foreground"
              }`}
            >
              <ArrowLeft className="h-3.5 w-3.5 shrink-0" />
              <span>Current version</span>
            </button>

            <div className="mx-4 my-1 border-t border-border/50" />

            {/* Version list */}
            {versions.map((v) => (
              <button
                key={v.version_number}
                onClick={() => onSelectVersion(v.version_number)}
                className={`flex flex-col gap-0.5 w-full px-4 py-2.5 text-left transition-colors ${
                  activeVersionNumber === v.version_number
                    ? "bg-primary/10 text-primary"
                    : "text-foreground hover:bg-muted"
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className="flex items-center gap-1.5 text-sm font-medium">
                    Version {v.version_number}
                    {v.target_lang && (
                      <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
                        {v.target_lang}
                      </span>
                    )}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {v.page_count} pages
                  </span>
                </div>
                <span className="text-xs text-muted-foreground" title={formatDate(v.generated_at)}>
                  Generated {getTimeAgo(v.generated_at)}
                </span>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
