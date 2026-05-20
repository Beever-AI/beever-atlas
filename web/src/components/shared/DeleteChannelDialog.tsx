import { useState } from "react";
import { AlertTriangle, Loader2, X as XIcon } from "lucide-react";
import { useDeleteChannel } from "@/hooks/useDeleteChannel";
import { ToastViewport } from "@/components/settings/ToastViewport";
import { useToast } from "@/hooks/useToast";

export interface DeleteChannelDialogProps {
  open: boolean;
  channelId: string;
  channelName: string;
  /** "file" for imported-document channels, "chat" for message channels. Defaults to chat copy. */
  channelKind?: "file" | "chat";
  /** Whether a sync is currently running for this channel. */
  isSyncing?: boolean;
  onClose: () => void;
  /** Called after a successful (200) or partial (207) delete with the channel id. */
  onDeleted: (channelId: string) => void;
}

function getBodyCopy(channelName: string, channelKind: "file" | "chat" | undefined): string {
  if (channelKind === "file") {
    return `This permanently deletes the imported documents, generated wiki, and derived knowledge for ${channelName}. Uploaded files in storage may remain.`;
  }
  // chat (default) — applies to every message platform (Slack, Discord,
  // Mattermost, Teams, Telegram). Phrased so it's accurate for both
  // channel-listing platforms (the channel reappears as available to ingest)
  // and webhook-only ones (it simply won't be re-ingested): we promise only
  // what's universally true — the ingested data is removed, the channel itself
  // is not, and a channel that still exists upstream may reappear.
  return `This permanently deletes all synced messages, Q&A, the wiki, and derived knowledge for ${channelName}, and unlinks it so it won't re-sync. It removes the ingested data only — it can't delete the channel from the connected platform, so if the channel still exists there it may reappear as available to ingest.`;
}

/**
 * Reusable type-to-confirm modal for hard-purging a channel.
 *
 * Mirrors the Reset & Re-sync danger-zone pattern in ChannelSettingsTab:
 * the user must type the channel display name exactly before the
 * destructive button enables. Shows an amber warning when a sync is
 * running. 207 partial-purge calls onDeleted (reaper converges).
 */
export function DeleteChannelDialog({
  open,
  channelId,
  channelName,
  channelKind,
  isSyncing,
  onClose,
  onDeleted,
}: DeleteChannelDialogProps) {
  const [confirmText, setConfirmText] = useState("");
  const { remove, loading } = useDeleteChannel();
  const toast = useToast();

  const confirmMatch =
    confirmText.trim() === channelName.trim() && confirmText.trim().length > 0;

  function handleClose() {
    if (loading) return;
    setConfirmText("");
    onClose();
  }

  async function handleConfirm() {
    if (!confirmMatch || loading) return;
    try {
      const result = await remove(channelId, channelName);
      setConfirmText("");
      // Both "completed" and "partial" (207) call onDeleted — channel is
      // gone enough from the user's perspective and the reaper converges.
      if (result.status !== "already_in_progress") {
        onDeleted(channelId);
      } else {
        // already_in_progress — close the dialog but don't fire onDeleted
        // since nothing was actually purged yet.
        toast.show(result.message ?? "A delete is already running for this channel.", "info");
        onClose();
      }
    } catch {
      // useDeleteChannel already set error state and showed an error toast.
      // Keep the dialog open so the user can retry or cancel.
    }
  }

  if (!open) return null;

  const bodyCopy = getBodyCopy(channelName, channelKind);

  return (
    <>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="delete-channel-dialog-title"
        className="fixed inset-0 z-40 flex items-center justify-center bg-background/80 backdrop-blur-sm"
        onClick={handleClose}
      >
        <div
          className="w-full max-w-md mx-4 rounded-2xl border border-border bg-card shadow-xl p-5 space-y-4"
          onClick={(e) => e.stopPropagation()}
        >
          {/* Header */}
          <div className="flex items-start justify-between gap-2">
            <div className="flex items-center gap-2">
              <AlertTriangle className="h-4 w-4 text-destructive" />
              <h3
                id="delete-channel-dialog-title"
                className="text-sm font-semibold text-foreground"
              >
                Delete channel permanently?
              </h3>
            </div>
            <button
              type="button"
              onClick={handleClose}
              disabled={loading}
              aria-label="Close"
              className="opacity-70 hover:opacity-100 disabled:opacity-30"
            >
              <XIcon className="h-4 w-4" />
            </button>
          </div>

          {/* Body copy */}
          <p className="text-[12px] leading-relaxed text-muted-foreground">
            {bodyCopy}
          </p>

          {/* Syncing warning */}
          {isSyncing && (
            <div
              data-testid="syncing-warning"
              className="flex items-center gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2"
            >
              <AlertTriangle className="h-3.5 w-3.5 text-amber-500 shrink-0" />
              <p className="text-[12px] text-amber-700 dark:text-amber-400">
                A sync is running. Deleting will cancel it.
              </p>
            </div>
          )}

          {/* Type-to-confirm input */}
          <div className="space-y-1.5">
            <label
              className="text-[11px] text-muted-foreground"
              htmlFor="delete-channel-confirm-input"
            >
              Type the channel name to confirm:{" "}
              <span className="font-mono text-foreground">{channelName}</span>
            </label>
            <input
              id="delete-channel-confirm-input"
              type="text"
              value={confirmText}
              onChange={(e) => setConfirmText(e.target.value)}
              disabled={loading}
              placeholder={channelName}
              autoFocus
              className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-destructive/40"
            />
          </div>

          {/* Actions */}
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={handleClose}
              disabled={loading}
              className="px-3 py-1.5 rounded-lg border border-border text-sm text-muted-foreground hover:bg-muted hover:text-foreground transition-colors disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleConfirm}
              disabled={!confirmMatch || loading}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-destructive text-destructive-foreground text-sm font-medium hover:bg-destructive/90 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {loading && <Loader2 size={14} className="animate-spin" />}
              {loading ? "Deleting..." : "Delete permanently"}
            </button>
          </div>
        </div>
      </div>

      <ToastViewport toasts={toast.toasts} onDismiss={toast.dismiss} />
    </>
  );
}
