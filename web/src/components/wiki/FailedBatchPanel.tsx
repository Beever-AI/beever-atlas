import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, Loader2, X } from "lucide-react";
import { api } from "@/lib/api";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface ExtractionFailure {
  message_id: string;
  next_attempt_at: string | null;
  attempt_count: number;
  last_error: string;
}

interface FailuresResponse {
  items: ExtractionFailure[];
  next_cursor: string | null;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function relativeTime(ts: string | null): string {
  if (!ts) return "—";
  try {
    const delta = new Date(ts).getTime() - Date.now();
    const abs = Math.abs(delta);
    const mins = Math.round(abs / 60000);
    const hrs = Math.round(abs / 3600000);
    const days = Math.round(abs / 86400000);
    const future = delta > 0;
    if (abs < 60000) return future ? "in a moment" : "just now";
    if (abs < 3600000) return future ? `in ${mins}m` : `${mins}m ago`;
    if (abs < 86400000) return future ? `in ${hrs}h` : `${hrs}h ago`;
    return future ? `in ${days}d` : `${days}d ago`;
  } catch {
    return ts;
  }
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface Props {
  channelId: string;
  onClose?: () => void;
}

export function FailedBatchPanel({ channelId, onClose }: Props) {
  const [items, setItems] = useState<ExtractionFailure[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchPage = useCallback(
    async (afterCursor: string | null, append: boolean) => {
      const isFirst = !append;
      if (isFirst) setLoading(true);
      else setLoadingMore(true);
      setError(null);
      try {
        const params = new URLSearchParams({ limit: "50" });
        if (afterCursor) params.set("cursor", afterCursor);
        const resp = await api.get<FailuresResponse>(
          `/api/channels/${channelId}/extraction-failures?${params.toString()}`,
        );
        setItems((prev) => (append ? [...prev, ...resp.items] : resp.items));
        setCursor(resp.next_cursor);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load failures");
      } finally {
        if (isFirst) setLoading(false);
        else setLoadingMore(false);
      }
    },
    [channelId],
  );

  useEffect(() => {
    void fetchPage(null, false);
  }, [fetchPage]);

  function handleLoadMore() {
    if (cursor) void fetchPage(cursor, true);
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Failed extractions"
      className="fixed right-4 top-20 z-30 w-[420px] max-h-[70vh] flex flex-col rounded-lg border border-border bg-background shadow-lg"
    >
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-border shrink-0">
        <h3 className="text-sm font-semibold text-foreground">
          Failed extractions
        </h3>
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            className="p-0.5 rounded hover:bg-muted transition-colors"
            aria-label="Close panel"
          >
            <X size={14} />
          </button>
        )}
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto">
        {loading ? (
          <div className="flex items-center justify-center py-10 gap-2 text-muted-foreground">
            <Loader2 size={16} className="animate-spin" />
            <span className="text-sm">Loading…</span>
          </div>
        ) : error ? (
          <div className="px-4 py-6 text-sm text-rose-600 dark:text-rose-400">
            {error}
          </div>
        ) : items.length === 0 ? (
          <div
            data-testid="failed-batch-empty-state"
            className="flex flex-col items-center justify-center py-10 text-center px-4"
          >
            <CheckCircle2 size={24} className="text-muted-foreground/50 mb-2" />
            <p className="text-sm text-muted-foreground">
              No failed extractions in the last 7 days.
            </p>
          </div>
        ) : (
          <ul className="divide-y divide-border" data-testid="failed-batch-list">
            {items.map((item, i) => (
              <li
                key={`${item.message_id}-${i}`}
                className="px-4 py-3 space-y-0.5"
                data-testid="failed-batch-row"
              >
                <div className="flex items-center justify-between gap-2">
                  <code className="text-xs font-mono text-foreground truncate max-w-[200px]">
                    {item.message_id}
                  </code>
                  <span className="text-[11px] text-muted-foreground shrink-0">
                    {item.attempt_count} attempt{item.attempt_count !== 1 ? "s" : ""}
                  </span>
                </div>
                <div className="flex items-center justify-between gap-2">
                  <p className="text-[11px] text-muted-foreground truncate" title={item.last_error}>
                    {item.last_error.length > 120
                      ? `${item.last_error.slice(0, 120)}…`
                      : item.last_error}
                  </p>
                  <span className="text-[11px] text-muted-foreground shrink-0">
                    retry {relativeTime(item.next_attempt_at)}
                  </span>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Load more */}
      {!loading && cursor && (
        <div className="px-4 py-3 border-t border-border shrink-0">
          <button
            type="button"
            onClick={handleLoadMore}
            disabled={loadingMore}
            data-testid="load-more-button"
            className="w-full inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs rounded-md border border-border hover:bg-muted transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {loadingMore && <Loader2 size={12} className="animate-spin" />}
            {loadingMore ? "Loading…" : "Load more"}
          </button>
        </div>
      )}
    </div>
  );
}
