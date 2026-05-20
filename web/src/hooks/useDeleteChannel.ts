import { useCallback, useState } from "react";
import { api, ApiError } from "@/lib/api";
// ApiError is used in the catch block for error message extraction.
import { useSyncStatus } from "@/contexts/SyncStatusContext";
import { useToast } from "@/hooks/useToast";

export interface DeleteChannelResult {
  channel_id: string;
  counts?: Record<string, number>;
  errors?: Record<string, string>;
  unlinked_from?: string[];
  sync_cancelled?: boolean;
  purge_run_id?: string;
  status: "completed" | "partial" | "already_in_progress";
  message?: string;
}

export interface UseDeleteChannelReturn {
  remove: (channelId: string, channelName: string) => Promise<DeleteChannelResult>;
  loading: boolean;
  error: string | null;
}

/**
 * Hook for hard-purging a channel via DELETE /api/channels/{id}?confirm=<name>.
 *
 * 207 handling: Response.ok is true for all 2xx including 207, so api.delete
 * returns the body normally for 207. We branch on result.status === "partial"
 * in the success path to surface a warning toast. Side-effects (release +
 * connections-changed) fire for both "completed" and "partial" — the channel
 * is gone enough from the user's perspective and the reaper converges.
 *
 * 200 + status "already_in_progress": no side-effects (nothing was purged).
 */
export function useDeleteChannel(): UseDeleteChannelReturn {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { release } = useSyncStatus();
  const toast = useToast();

  const remove = useCallback(
    async (channelId: string, channelName: string): Promise<DeleteChannelResult> => {
      setLoading(true);
      setError(null);

      const url = `/api/channels/${encodeURIComponent(channelId)}?confirm=${encodeURIComponent(channelName)}`;

      try {
        const result = await api.delete<DeleteChannelResult>(url);

        if (result.status === "already_in_progress") {
          // A delete is already running — inform the user, no side-effects.
          toast.show(result.message ?? "A delete is already in progress for this channel.", "info");
          return result;
        }

        if (result.status === "partial") {
          // 207 Multi-Status: partial purge. Channel is mostly gone; the
          // reaper converges. Surface an informational notice (NOT an error —
          // it mostly succeeded and self-heals) but still fire side-effects so
          // the grid reflects the deletion. useToast only exposes "info" /
          // "error"; "info" is the closest non-alarming variant.
          toast.show(
            `Channel "${channelName}" partially deleted. Some data may remain; the system will clean it up automatically.`,
            "info",
          );
          release(channelId);
          window.dispatchEvent(new Event("connections-changed"));
          return result;
        }

        // status === "completed"
        release(channelId);
        window.dispatchEvent(new Event("connections-changed"));
        toast.show(`Channel "${channelName}" deleted.`, "info");
        return result;
      } catch (err: unknown) {
        // 404 = the channel's ingested footprint is already gone (a prior purge
        // that converged, the reaper finished, or a double-click). Treat it as
        // success: drop it from the grid and fire the same side-effects as a
        // clean delete instead of surfacing a scary error.
        if (err instanceof ApiError && err.status === 404) {
          release(channelId);
          window.dispatchEvent(new Event("connections-changed"));
          toast.show(`Channel "${channelName}" was already deleted.`, "info");
          return { channel_id: channelId, status: "completed" };
        }
        const msg =
          err instanceof ApiError
            ? err.message
            : err instanceof Error
              ? err.message
              : "Failed to delete channel";
        setError(msg);
        throw err;
      } finally {
        setLoading(false);
      }
    },
    [release, toast],
  );

  return { remove, loading, error };
}
