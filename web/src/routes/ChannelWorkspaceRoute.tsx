/**
 * ChannelWorkspaceRoute (RES-285)
 *
 * Thin wrapper that reads the `:id` route param via `useParams` and
 * passes it to `<ChannelWorkspace key={id} />`. The React `key` prop
 * forces a full subtree remount on every channelId change, which is
 * what makes the channel-switch state-leak class of bugs structurally
 * impossible:
 *
 *   - `useSync`'s `syncState` and `lastFingerprintRef`
 *   - `channel` / `loadingChannel` / `refreshing`
 *   - `cooldownRemaining`'s setInterval
 *
 * …all five state cells in `ChannelWorkspace` reset atomically on the
 * commit phase of the route param change, without each owner needing
 * its own ad-hoc reset effect.
 *
 * Why a wrapper rather than `<ChannelWorkspace key={id} />` inline in
 * `App.tsx:88`? React Router's `<Route element>` JSX has no access to
 * the path params at the declaration site — `:id` is only resolved
 * inside a descendant via `useParams`. The wrapper is the smallest
 * idiomatic adapter.
 */

import { useParams } from "react-router-dom";
import { ChannelWorkspace } from "@/pages/ChannelWorkspace";

export function ChannelWorkspaceRoute() {
  const { id } = useParams<{ id: string }>();
  // React Router shouldn't render this route without `:id`, but stay
  // defensive — returning null lets the parent route fall through.
  if (!id) return null;
  return <ChannelWorkspace key={id} />;
}
