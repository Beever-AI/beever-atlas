import { useState, useCallback } from "react";
import { api } from "@/lib/api";

interface RegenerateState {
  isPending: boolean;
  error: Error | null;
  succeeded: boolean;
}

/** Hook for the "Retry overview generation" affordance shown when the
 *  WikiTab's loading screen has been stuck for too long.
 *
 *  Wraps ``POST /api/channels/{id}/wiki/regenerate-overview`` which
 *  force-resets the AutoOverviewSubscriber's in-flight state and
 *  re-triggers generation. The endpoint is idempotent — calling it
 *  while a build is genuinely running is safe; the gate-check inside
 *  the subscriber prevents a duplicate parallel build.
 *
 *  ``succeeded`` flips true for ~3 seconds after a successful retry so
 *  the WikiTab can flash a brief confirmation message without needing
 *  a toast library.
 */
export function useRegenerateOverview(channelId: string | undefined) {
  const [state, setState] = useState<RegenerateState>({
    isPending: false,
    error: null,
    succeeded: false,
  });

  const regenerate = useCallback(async () => {
    if (!channelId) return;
    setState({ isPending: true, error: null, succeeded: false });
    try {
      await api.post(`/api/channels/${channelId}/wiki/regenerate-overview`);
      setState({ isPending: false, error: null, succeeded: true });
      // Clear the success flag after a short delay so the inline
      // confirmation doesn't stick around forever.
      setTimeout(() => {
        setState((prev) =>
          prev.succeeded ? { ...prev, succeeded: false } : prev,
        );
      }, 3000);
    } catch (err) {
      setState({
        isPending: false,
        error: err instanceof Error ? err : new Error(String(err)),
        succeeded: false,
      });
    }
  }, [channelId]);

  return { ...state, regenerate };
}
