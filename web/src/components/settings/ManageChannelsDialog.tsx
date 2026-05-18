import { useState, useEffect } from "react";
import { X, Loader2, AlertCircle, Save, RefreshCw } from "lucide-react";
import { ChannelSelector } from "./ChannelSelector";
import { useConnectionChannels, useUpdateChannels } from "@/hooks/useConnections";
import type { PlatformConnection } from "@/lib/types";

interface ManageChannelsDialogProps {
  connection: PlatformConnection;
  onClose: () => void;
}

export function ManageChannelsDialog({ connection, onClose }: ManageChannelsDialogProps) {
  const { channels, loading, error, refetch } = useConnectionChannels(connection.id);
  const { updateChannels, loading: saving, error: saveError } = useUpdateChannels(connection.id);
  const [selected, setSelected] = useState<string[]>(connection.selected_channels);

  useEffect(() => {
    setSelected(connection.selected_channels);
  }, [connection.selected_channels]);

  async function handleSave() {
    try {
      await updateChannels(selected);
      onClose();
    } catch {
      // error surfaced via saveError
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" onClick={onClose} />

      {/* Dialog */}
      <div className="relative z-10 w-full max-w-lg bg-card border border-border rounded-2xl shadow-2xl overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-border">
          <div>
            <h2 className="text-base font-semibold text-foreground">Manage Channels</h2>
            <p className="text-xs text-muted-foreground mt-0.5">
              {connection.display_name || connection.platform}
            </p>
          </div>
          <div className="flex items-center gap-1">
            {/* RES-286 — refresh button. Calling refetch() re-fetches the bot's
                live channel list, so a channel the user just added the bot to
                (e.g. tech-studio on Mattermost) shows up without restarting the
                bot or reopening the dialog. */}
            <button
              type="button"
              onClick={refetch}
              disabled={loading}
              title="Refresh channel list"
              aria-label="Refresh channel list"
              className="w-8 h-8 flex items-center justify-center rounded-lg hover:bg-muted transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <RefreshCw
                className={`w-4 h-4 text-muted-foreground ${loading ? "animate-spin" : ""}`}
              />
            </button>
            <button
              type="button"
              onClick={onClose}
              className="w-8 h-8 flex items-center justify-center rounded-lg hover:bg-muted transition-colors"
            >
              <X className="w-4 h-4 text-muted-foreground" />
            </button>
          </div>
        </div>

        {/* Content */}
        <div className="px-6 py-5">
          {loading ? (
            <div className="flex items-center justify-center py-10">
              <Loader2 className="w-6 h-6 text-primary animate-spin" />
            </div>
          ) : error ? (
            <div className="flex items-center gap-2 rounded-lg bg-rose-500/10 border border-rose-500/20 px-3 py-2.5">
              <AlertCircle className="w-4 h-4 text-rose-500 shrink-0" />
              <p className="text-xs text-rose-600 dark:text-rose-400">{error}</p>
            </div>
          ) : (
            <ChannelSelector channels={channels} selected={selected} onChange={setSelected} />
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between px-6 py-4 border-t border-border bg-muted/30">
          <div>
            {saveError && (
              <div className="flex items-center gap-1.5 text-xs text-rose-600 dark:text-rose-400">
                <AlertCircle className="w-3.5 h-3.5" />
                {saveError}
              </div>
            )}
          </div>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={onClose}
              className="px-3 py-1.5 rounded-lg border border-border text-sm font-medium text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleSave}
              disabled={saving || loading}
              className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50"
            >
              {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
              Save
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
