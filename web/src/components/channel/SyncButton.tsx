import { RefreshCw, CheckCircle, AlertCircle } from "lucide-react";
import { useState, useEffect } from "react";
import { cn } from "@/lib/utils";
import type { SyncState } from "@/hooks/useSync";

interface SyncButtonProps {
  syncState: SyncState;
  isSyncing: boolean;
  error: string | null;
  onSync: () => void;
}

export function SyncButton({ syncState, isSyncing, error, onSync }: SyncButtonProps) {
  const [justCompleted, setJustCompleted] = useState(false);
  const effectiveError = error || syncState.errors?.filter(Boolean).join("; ") || null;

  useEffect(() => {
    if (syncState.state === "idle" && syncState.job_id) {
      setJustCompleted(true);
      const timer = setTimeout(() => setJustCompleted(false), 3000);
      return () => clearTimeout(timer);
    }
  }, [syncState.state, syncState.job_id]);

  if (justCompleted) {
    return (
      <button
        disabled
        className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium bg-green-500/10 text-green-600 dark:text-green-400"
      >
        <CheckCircle className="w-3.5 h-3.5" />
        {(syncState.total_messages ?? 0) > 0 ? "Synced" : "No new messages"}
      </button>
    );
  }

  if (syncState.state === "error" || effectiveError) {
    return (
      <button
        onClick={onSync}
        className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium bg-red-500/10 text-red-600 dark:text-red-400 hover:bg-red-500/20"
        title={effectiveError ?? "Sync failed"}
      >
        <AlertCircle className="w-3.5 h-3.5" />
        Retry Sync
      </button>
    );
  }

  return (
    <button
      onClick={onSync}
      disabled={isSyncing}
      className={cn(
        "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors",
        isSyncing
          ? "bg-muted text-muted-foreground cursor-not-allowed"
          : "bg-primary/10 text-primary hover:bg-primary/20",
      )}
    >
      <RefreshCw className={cn("w-3.5 h-3.5", isSyncing && "animate-spin")} />
      {isSyncing ? "Syncing..." : "Sync Channel"}
    </button>
  );
}
