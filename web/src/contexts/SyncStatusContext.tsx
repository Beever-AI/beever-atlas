/**
 * SyncStatusContext (RES-285)
 *
 * Mirrors the derived "is any channel syncing right now?" signal out of
 * `ChannelWorkspace` into a shared scope so `Sidebar` can gate its
 * top-nav NavLinks without subscribing to the polling hook directly.
 *
 * Design notes (from the ralplan consensus loop):
 *
 *  - **Two `useState` cells, not one object.** Splitting the boolean and
 *    the string keeps React's `Object.is` bail-out in play: identical
 *    publishes (e.g. `setIsSyncRunning(true)` while already `true`)
 *    short-circuit and consumers don't re-render. Wrapping the same
 *    values in a single object literal would create a fresh identity on
 *    every publish and break AC6.
 *  - **`setIsSyncRunning` / `setChannelId` are the raw React setters** —
 *    referentially stable for the life of the provider, safe to put in
 *    useEffect dependency arrays.
 *  - **Gate scope: ONLY `state === "syncing"`.** Error / idle / completed
 *    DO NOT gate the nav. Rationale: error is terminal and the fix path
 *    usually goes through Settings — gating Settings would trap the
 *    user with no recovery. The publisher in `ChannelWorkspace` is
 *    responsible for narrowing `syncState.state` to that boolean.
 *  - **Home is intentionally excluded from the gate** (universal escape
 *    hatch). The Sidebar consumer is the policy site.
 */

import {
  type Dispatch,
  type ReactNode,
  type SetStateAction,
  createContext,
  useContext,
  useMemo,
  useState,
} from "react";

export interface SyncStatusContextValue {
  /** True iff some channel's `useSync` reports `state === "syncing"`. */
  isSyncRunning: boolean;
  /** The channelId currently syncing, or null when idle. Surfaced so the
   *  nav-gate tooltip can name it ("Sync in progress on #marketing…"). */
  channelId: string | null;
  setIsSyncRunning: Dispatch<SetStateAction<boolean>>;
  setChannelId: Dispatch<SetStateAction<string | null>>;
}

const SyncStatusContext = createContext<SyncStatusContextValue | null>(null);

interface SyncStatusProviderProps {
  children: ReactNode;
}

export function SyncStatusProvider({ children }: SyncStatusProviderProps) {
  const [isSyncRunning, setIsSyncRunning] = useState(false);
  const [channelId, setChannelId] = useState<string | null>(null);

  // useMemo deps are PRIMITIVES (boolean + string|null), so this memo
  // only invalidates when a real value change happens — preserving the
  // `Object.is` bail-out path through to subscribers.
  const value = useMemo<SyncStatusContextValue>(
    () => ({ isSyncRunning, channelId, setIsSyncRunning, setChannelId }),
    [isSyncRunning, channelId],
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
