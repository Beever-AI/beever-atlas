/**
 * SyncStatusContext (RES-285 + follow-ups)
 *
 * Single source of truth for "which channels are currently syncing?"
 * The Sidebar reads from here to (a) gate the top-nav NavLinks while
 * any sync is active and (b) paint a pulsing dot on the row of each
 * syncing channel — both of which must work from ANY page in the app,
 * not just from inside a syncing channel's own workspace.
 *
 * Design notes:
 *
 *  - **`syncingChannels: Set<string>` instead of a single channel id.**
 *    The architecture must support concurrent syncs across multiple
 *    channels — even if the backend currently throttles to one at a
 *    time, this design lets that constraint relax in the future
 *    without any FE rewrite. Consumers derive their own boolean flags
 *    (`syncingChannels.size > 0` for "is anything syncing",
 *    `syncingChannels.has(id)` for "is THIS channel syncing").
 *  - **Claim / release publisher protocol.** `ChannelWorkspace` calls
 *    `claim(myId)` when its sync starts and `release(myId)` when it
 *    ends. Other channels' publishers never touch each other's slots,
 *    so cross-channel navigation cannot clobber the indicator.
 *  - **`claim`/`release` are stable `useCallback`s** with empty deps —
 *    safe in `useEffect` dependency arrays without retriggering on
 *    every render.
 *  - **Background poller as the long-running source of truth.** Since
 *    `ChannelWorkspace` unmounts when the user navigates away from a
 *    syncing channel, we can't rely on it to detect sync completion
 *    from any other page. The Provider polls each tracked channel's
 *    `/api/channels/{id}/sync/status` every 5 s and releases ids when
 *    the backend reports no active sync. Polling stops when the set
 *    is empty (no work to do).
 *  - **No `error` gating.** A sync in `error` state is terminal —
 *    locking the user out of Settings on error would block recovery.
 *    Both the publisher and the poller treat `error` as "not running".
 */

import {
  type ReactNode,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { api } from "@/lib/api";

export interface SyncStatusContextValue {
  /** Set of channel ids currently syncing. New `Set` instance on every
   *  add/remove (React `Object.is` triggers consumer re-renders on
   *  membership change; identity-stable when claim/release is a
   *  no-op). */
  syncingChannels: ReadonlySet<string>;
  /** Mark a channel as actively syncing. Idempotent. */
  claim: (channelId: string) => void;
  /** Mark a channel as no longer syncing. Idempotent. */
  release: (channelId: string) => void;
}

const SyncStatusContext = createContext<SyncStatusContextValue | null>(null);

interface SyncStatusProviderProps {
  children: ReactNode;
}

/** Poll interval for the global sync tracker (ms). Cheap — `N` requests
 *  per `5 s` only while syncs are active (`N = syncingChannels.size`);
 *  idle otherwise. */
const SYNC_POLL_INTERVAL_MS = 5000;

export function SyncStatusProvider({ children }: SyncStatusProviderProps) {
  const [syncingChannels, setSyncingChannels] = useState<ReadonlySet<string>>(
    () => new Set<string>(),
  );

  const claim = useCallback((channelId: string) => {
    setSyncingChannels((prev) => {
      if (prev.has(channelId)) return prev; // identity-stable no-op
      const next = new Set(prev);
      next.add(channelId);
      return next;
    });
  }, []);

  const release = useCallback((channelId: string) => {
    setSyncingChannels((prev) => {
      if (!prev.has(channelId)) return prev; // identity-stable no-op
      const next = new Set(prev);
      next.delete(channelId);
      return next;
    });
  }, []);

  // Global poller — the missing piece that makes the sidebar indicator
  // work from pages other than each syncing channel's own workspace.
  // Without this, navigating away mid-sync would leave the indicator
  // pinned on a channel that has since finished.
  useEffect(() => {
    if (syncingChannels.size === 0) return;
    let cancelled = false;
    // Snapshot the current set so a mid-tick claim/release doesn't
    // mutate the iteration we're walking. `useEffect` re-runs whenever
    // the set's identity changes, so we always poll the latest set.
    const snapshot = Array.from(syncingChannels);

    const tick = async () => {
      if (cancelled) return;
      for (const channelId of snapshot) {
        if (cancelled) return;
        try {
          const status = await api.get<{
            state?: string;
            phases?: Array<{ state?: string }>;
          }>(`/api/channels/${channelId}/sync/status`);
          if (cancelled) return;
          const phasesInFlight = (status.phases ?? []).some(
            (p) => p.state === "in_flight",
          );
          const stillRunning = status.state === "syncing" || phasesInFlight;
          if (!stillRunning) {
            release(channelId);
          }
        } catch {
          // Transient network errors are fine — the next tick will retry.
          // A 404 (channel deleted) would leave the id stuck; consumers
          // can clear it by navigating back to the workspace, or we can
          // add explicit 404 handling in a follow-up if it bites.
        }
      }
    };

    // Fire one immediately so the very first nav away from a syncing
    // channel doesn't wait 5 s for confirmation.
    void tick();
    const interval = setInterval(tick, SYNC_POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [syncingChannels, release]);

  const value = useMemo<SyncStatusContextValue>(
    () => ({ syncingChannels, claim, release }),
    [syncingChannels, claim, release],
  );

  return (
    <SyncStatusContext.Provider value={value}>{children}</SyncStatusContext.Provider>
  );
}

export function useSyncStatus(): SyncStatusContextValue {
  const ctx = useContext(SyncStatusContext);
  if (!ctx) {
    throw new Error("useSyncStatus must be used inside <SyncStatusProvider>");
  }
  return ctx;
}
