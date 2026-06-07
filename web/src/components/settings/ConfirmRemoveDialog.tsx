import { AlertTriangle, X } from "lucide-react";
import type { PlatformConnection } from "@/lib/types";

interface ConfirmRemoveDialogProps {
  connection: PlatformConnection;
  onCancel: () => void;
  onConfirm: (cascade: boolean) => void;
}

export function ConfirmRemoveDialog({ connection, onCancel, onConfirm }: ConfirmRemoveDialogProps) {
  const displayName = connection.display_name || connection.platform;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/40 backdrop-blur-sm"
        onClick={onCancel}
      />

      {/* Dialog */}
      <div className="relative z-10 flex flex-col w-full max-w-md bg-card border border-border rounded-2xl shadow-2xl overflow-hidden">
        {/* Header */}
        <div className="shrink-0 px-6 py-4 border-b border-border flex items-center justify-between">
          <div className="flex items-center gap-2">
            <AlertTriangle className="w-4 h-4 text-rose-500" />
            <h2 className="text-base font-semibold text-foreground">
              Remove {displayName}?
            </h2>
          </div>
          <button
            type="button"
            onClick={onCancel}
            className="w-8 h-8 flex items-center justify-center rounded-lg hover:bg-muted transition-colors"
          >
            <X className="w-4 h-4 text-muted-foreground" />
          </button>
        </div>

        {/* Body */}
        <div className="px-6 py-5 space-y-4">
          <p className="text-sm text-foreground">
            Removing this connection will disconnect{" "}
            <span className="font-medium">{displayName}</span> from Beever Atlas.
          </p>

          <div className="rounded-lg bg-rose-500/10 border border-rose-500/20 px-4 py-3 space-y-1.5">
            <p className="text-xs font-semibold text-rose-600 dark:text-rose-400 uppercase tracking-wide">
              Warning — data loss
            </p>
            <p className="text-sm text-foreground/80">
              This connection's channels may have synced messages, wiki pages, and facts. If those channels are{" "}
              <span className="font-medium">only used by this connection</span>, removing with data deletion will{" "}
              <span className="font-medium text-rose-600 dark:text-rose-400">permanently delete</span> all of that synced data.
            </p>
          </div>

          <p className="text-xs text-muted-foreground">
            If you want to preserve the synced data (e.g. to reconnect later or keep the wiki), choose{" "}
            <span className="font-medium text-foreground">Remove, keep data</span>.
          </p>
        </div>

        {/* Footer */}
        <div className="shrink-0 flex items-center justify-end gap-2 px-6 py-4 border-t border-border bg-muted/30 flex-wrap">
          <button
            type="button"
            onClick={onCancel}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-border text-sm font-medium text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => onConfirm(false)}
            className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-lg border border-border text-sm font-medium text-foreground hover:bg-muted transition-colors"
          >
            Remove, keep data
          </button>
          <button
            type="button"
            onClick={() => onConfirm(true)}
            className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-lg bg-rose-600 text-white text-sm font-medium hover:bg-rose-700 transition-colors"
          >
            Remove &amp; delete data
          </button>
        </div>
      </div>
    </div>
  );
}
