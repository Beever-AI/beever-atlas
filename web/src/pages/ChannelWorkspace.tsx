import { useEffect, useState, type ComponentType } from "react";
import { useParams, Outlet, useNavigate, useLocation, Link, Navigate } from "react-router-dom";
import { api } from "@/lib/api";
import {
  ArrowLeft,
  ShieldAlert,
  RefreshCw,
  MessageCircleQuestion,
  BookOpen,
  Brain,
  FileText,
  History,
  Settings,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useConnectionMap } from "@/hooks/useConnectionMap";
import { useRecentChannels } from "@/hooks/useRecentChannels";
import { ChannelBreadcrumb } from "@/components/channel/Breadcrumb";
import { SyncButton } from "@/components/channel/SyncButton";
import { SyncProgress } from "@/components/channel/SyncProgress";
import { NextSyncBadge } from "@/components/channel/NextSyncBadge";
import { useSync } from "@/hooks/useSync";
import { LanguageBadge } from "@/components/channel/LanguageBadge";
import { useSyncStatus } from "@/contexts/SyncStatusContext";

interface ChannelInfo {
  channel_id: string;
  name: string;
  platform: string;
  is_member?: boolean;
  member_count?: number | null;
  connection_id?: string | null;
  primary_language?: string | null;
  primary_language_confidence?: number | null;
}

interface ChannelRouteState {
  channel_name?: string;
  platform?: string;
  is_member?: boolean;
  member_count?: number | null;
  connection_id?: string | null;
}

// Tab order is intentional — Channel Wiki first so the breadcrumb
// fallback in ``getCurrentTab`` lands on a useful surface, then Agent
// Memory (which now hosts both the TierBrowser and the entity graph
// via ``?view=graph``), Messages, Sync History, Settings. Path
// segments stay stable so existing deep-links still resolve; only the
// labels were updated to match the new IA.
const TAB_PATHS = ["wiki", "memories", "messages", "sync-history", "settings"] as const;
type TabPath = (typeof TAB_PATHS)[number];

const TAB_LABELS: Record<TabPath, string> = {
  wiki: "Channel Wiki",
  memories: "Agent Memory",
  messages: "Source",
  "sync-history": "Sync History",
  // Renamed from "Settings" to disambiguate from the workspace-level
  // Settings entry in the left rail — operators were clicking the
  // wrong one and getting confused by the scope mismatch.
  settings: "Channel Settings",
};

// Lucide icon per tab. Rendered small (h-3.5) and inherits the row's
// text color so it acts as a visual anchor without competing with
// the label. Picked for specificity (Brain for memory, FileText for
// raw source) — generic glyphs (Settings cog, History clock) are
// kept because the rename "Channel Settings" + grouping already
// disambiguates them at the strip level.
const TAB_ICONS: Record<TabPath, ComponentType<{ className?: string }>> = {
  wiki: BookOpen,
  memories: Brain,
  messages: FileText,
  "sync-history": History,
  settings: Settings,
};

// Visual grouping for the desktop tab strip:
//   AI surfaces · raw data · operations
// A thin vertical divider sits between groups so the eye can tell
// "things I read" from "the source" from "things I configure" at a
// glance — without needing labels to call out the buckets.
const TAB_GROUPS: TabPath[][] = [
  ["wiki", "memories"],
  ["messages"],
  ["sync-history", "settings"],
];

// Platform → swatch color for the scope chip. Swatches keep the chip
// readable at small sizes without pulling in brand SVGs (cuts bundle
// weight). New platforms fall back to a neutral slate dot.
const PLATFORM_DOT: Record<string, string> = {
  slack: "bg-fuchsia-500",
  mattermost: "bg-violet-500",
  discord: "bg-indigo-500",
  teams: "bg-sky-500",
  telegram: "bg-cyan-500",
  file: "bg-slate-500",
};

function getCurrentTab(pathname: string): TabPath {
  const segment = pathname.split("/").at(-1) as TabPath;
  return TAB_PATHS.includes(segment) ? segment : "wiki";
}

export function ChannelWorkspace() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const routeState = (location.state as ChannelRouteState | null) ?? null;
  const [channel, setChannel] = useState<ChannelInfo | null>(() => {
    if (!id || !routeState?.channel_name) return null;
    return {
      channel_id: id,
      name: routeState.channel_name,
      platform: routeState.platform || "slack",
      is_member: routeState.is_member ?? false,
      member_count: routeState.member_count ?? null,
      connection_id: routeState.connection_id ?? null,
    };
  });
  const [refreshing, setRefreshing] = useState(false);
  const [loadingChannel, setLoadingChannel] = useState(!routeState?.channel_name);
  // Monitor collapse state lives here so the workspace layout can react
  // to it — when collapsed, the monitor renders as a compact strip
  // and the wiki content below stays visible; when expanded on the
  // wiki tab, the monitor fills the page fullscreen and the body is
  // hidden. Hydrated from the same localStorage key SyncProgressV2
  // wrote to in earlier uncontrolled mode.
  const [monitorCollapsed, setMonitorCollapsed] = useState<boolean>(() => {
    // Default: collapsed. Users find the bar arresting when expanded by
    // default; once they've explicitly clicked Expand once, we remember
    // it. `raw === "false"` is the only signal we treat as opt-in-to-
    // expanded; anything else (null, "true", junk) → collapsed.
    if (typeof window === "undefined") return true;
    try {
      const raw = window.localStorage.getItem("beever.monitor.collapsed");
      return raw !== "false";
    } catch {
      return true;
    }
  });
  useEffect(() => {
    try {
      window.localStorage.setItem(
        "beever.monitor.collapsed",
        JSON.stringify(monitorCollapsed),
      );
    } catch {
      /* ignore quota errors */
    }
  }, [monitorCollapsed]);
  const { getWorkspaceName } = useConnectionMap();
  const { trackVisit } = useRecentChannels();

  const activeTab = getCurrentTab(location.pathname);

  // Track this channel as a recent visit so the dashboard's "Pick up
  // where you left off" section surfaces it next time the user lands
  // on /home. Single fire per channel resolution — trackVisit dedupes
  // by channel_id internally, so a tab switch within the workspace
  // doesn't bump the same entry repeatedly.
  useEffect(() => {
    if (!id || !channel?.name) return;
    trackVisit({
      channel_id: id,
      name: channel.name,
      platform: channel.platform || "unknown",
    });
  }, [id, channel?.name, channel?.platform, trackVisit]);

  useEffect(() => {
    if (!id) return;
    if (routeState?.channel_name) {
      setChannel((prev) => ({
        channel_id: id,
        name: routeState.channel_name || prev?.name || "Channel",
        platform: routeState.platform || prev?.platform || "slack",
        is_member: routeState.is_member ?? prev?.is_member ?? false,
        member_count: routeState.member_count ?? prev?.member_count ?? null,
        connection_id: routeState.connection_id ?? prev?.connection_id ?? null,
        primary_language: prev?.primary_language ?? null,
        primary_language_confidence: prev?.primary_language_confidence ?? null,
      }));
    }
    // Don't send route-state connection_id — it may be stale (wrong workspace).
    // Let the backend resolve the correct connection; the response will contain
    // the authoritative connection_id for all subsequent requests.
    setLoadingChannel(true);
    api
      .get<ChannelInfo>(`/api/channels/${id}`)
      .then(setChannel)
      .catch(() =>
        setChannel((prev) =>
          prev ?? {
            channel_id: id,
            name: "Channel",
            platform: "slack",
            is_member: false,
          }
        )
      )
      .finally(() => setLoadingChannel(false));
  }, [id, routeState?.channel_name, routeState?.platform, routeState?.member_count, routeState?.connection_id]);

  function handleTabChange(value: string) {
    navigate(`/channels/${id}/${value}`);
  }

  const isMember = channel?.is_member === true;
  const { syncState, triggerSync, isSyncing, error: syncError } = useSync(id ?? "", channel?.connection_id ?? null);

  // RES-285 + follow-up — publish THIS channel's sync state to the
  // shared SyncStatusContext so Sidebar (top-nav gate + per-row
  // indicator) reflects it from ANY page in the app, not just from
  // inside this workspace.
  //
  // Claim / release model — we only mark OURSELVES; other channels'
  // publishers manage their own slots. Concurrent syncs across
  // multiple channels are fully supported: each channel's
  // ChannelWorkspace publishes independently, and the Provider's
  // background poller releases stale ids when their syncs complete.
  //
  // Active-sync detection mirrors `useSync.ts:300-304`: the backend
  // may return `state: "idle"` while phases are still `in_flight`
  // (the "warming up" window after dispatch). Both signals count.
  // `error` is excluded — terminal state, gating the nav on error
  // would trap users away from Settings.
  //
  // NO unmount cleanup. If we cleared on unmount, the sidebar
  // indicator for an actively-syncing channel would disappear the
  // moment the user navigated away — defeating the point. The
  // Provider's poller is responsible for the eventual release.
  const { claim: claimSync, release: releaseSync } = useSyncStatus();
  const anyPhaseInFlight = (syncState.phases ?? []).some(
    (p) => p.state === "in_flight",
  );
  const isSyncRunningHere =
    syncState.state === "syncing" || anyPhaseInFlight;
  useEffect(() => {
    if (!id) return;
    if (isSyncRunningHere) {
      claimSync(id);
    } else {
      // Idempotent release — release() is a no-op if we don't hold the
      // slot, so this is safe to call on every render where we're not
      // syncing (it's the "I'm done" signal from THIS channel only).
      releaseSync(id);
    }
  }, [isSyncRunningHere, id, claimSync, releaseSync]);
  // PR-B: when the failure banner copy is built from sync state errors,
  // prefer the deduped form (single line per unique message + count)
  // so a Gemini 503 storm shows "AI provider temporarily unavailable
  // (×12 batches)" instead of twelve repeated lines.
  const syncFailureMessage = (() => {
    if (syncError) return syncError;
    if (syncState.state !== "error") return null;
    if (syncState.dedupedErrors && syncState.dedupedErrors.length > 0) {
      return syncState.dedupedErrors
        .map((e) => (e.count > 1 ? `${e.message} (×${e.count} batches)` : e.message))
        .join("; ");
    }
    return syncState.errors?.filter(Boolean).join("; ") ?? null;
  })();

  // Parse "Try again in Ns." out of cooldown errors so we can show a live
  // countdown instead of a stale number. Matched once when the message arrives;
  // ticks down via setInterval without refetching.
  const cooldownSecondsFromMsg = (() => {
    if (!syncFailureMessage) return null;
    const m = /Try again in (\d+)s/.exec(syncFailureMessage);
    return m ? parseInt(m[1]!, 10) : null;
  })();
  const [cooldownRemaining, setCooldownRemaining] = useState<number | null>(null);
  useEffect(() => {
    if (cooldownSecondsFromMsg == null) {
      setCooldownRemaining(null);
      return;
    }
    setCooldownRemaining(cooldownSecondsFromMsg);
    const t = setInterval(() => {
      setCooldownRemaining((prev) => {
        if (prev == null || prev <= 1) {
          clearInterval(t);
          return null;
        }
        return prev - 1;
      });
    }, 1000);
    return () => clearInterval(t);
  }, [cooldownSecondsFromMsg]);
  const isCoolingDown = cooldownRemaining != null && cooldownRemaining > 0;
  const displayFailureMessage = isCoolingDown
    ? `Cooldown active. Try again in ${cooldownRemaining}s.`
    : syncFailureMessage;
  const syncCompletedWithNoNew =
    syncState.state === "idle" && !!syncState.job_id && (syncState.total_messages ?? 0) === 0;

  function handleRefreshStatus() {
    if (!id) return;
    setRefreshing(true);
    const connParam = channel?.connection_id ? `?connection_id=${channel.connection_id}` : "";
    api
      .get<ChannelInfo>(`/api/channels/${id}${connParam}`)
      .then((data) => setChannel(data))
      .catch(() => {})
      .finally(() => setRefreshing(false));
  }

  const channelDisplayName = (channel?.name ?? "channel").replace(/^#/, "");

  const platformInstructions: Record<string, { steps: string[]; botName: string }> = {
    slack: {
      botName: "@beever",
      steps: [
        `Open #${channelDisplayName} in Slack`,
        "Type /invite @beever or click channel name → Integrations → Add apps",
        "Come back here and click Refresh Status",
      ],
    },
    teams: {
      botName: "Beever Atlas",
      steps: [
        `Open the ${channelDisplayName} channel in Teams`,
        "Click the + icon → Manage apps → Add Beever Atlas",
        "Come back here and click Refresh Status",
      ],
    },
    discord: {
      botName: "Beever Atlas",
      steps: [
        "Open Server Settings → Integrations in Discord",
        "Find Beever Atlas and ensure it has access to this channel",
        "Come back here and click Refresh Status",
      ],
    },
  };

  const instructions = platformInstructions[channel?.platform ?? "slack"] ?? platformInstructions.slack;

  // A sync is considered active when state is syncing/error OR while
  // the EXTRACTING phase is still pending/in_flight. Once extraction
  // completes (extracting=done), the wiki is browsable in the
  // background and the monitor auto-hides — the user no longer needs
  // to see the progress bar even if wiki_maintenance/overview_wiki
  // are still finalizing in the background. This drops the "monitor
  // bar still here when wiki is generated" complaint from UI testing.
  const _phaseByName = Object.fromEntries(
    (syncState.phases ?? []).map((p) => [p.name, p]),
  );
  const _extracting = _phaseByName["extracting"]?.state;
  const _fetched = _phaseByName["fetched"]?.state;
  const pipelineActive =
    syncState.state === "syncing" ||
    syncState.state === "error" ||
    _fetched === "in_flight" ||
    _fetched === "pending" ||
    _extracting === "in_flight" ||
    _extracting === "pending";

  return (
    <div className="flex flex-col h-full min-h-0">
      {/* Compact channel bar: title + tabs in one layer */}
      <div className="shrink-0 px-3 sm:px-6 py-2 sm:py-2.5 border-b border-border bg-background">
        <div className="flex flex-col gap-2 sm:gap-2.5">
          <div className="flex items-center gap-2.5 sm:gap-3 min-w-0">
            <Link
              to="/channels"
              className="flex items-center justify-center w-8 h-8 rounded-lg hover:bg-muted transition-colors shrink-0"
            >
              <ArrowLeft className="w-4 h-4 text-muted-foreground" />
            </Link>
            <ChannelBreadcrumb
              workspace={getWorkspaceName(channel?.connection_id ?? null)}
              platform={channel?.platform ?? ""}
              channelName={channel?.name ?? "Loading..."}
              channelId={id ?? ""}
              activeTab={TAB_LABELS[activeTab]}
              connectionId={channel?.connection_id ?? null}
            />
            <LanguageBadge lang={channel?.primary_language ?? null} confidence={channel?.primary_language_confidence ?? null} />
            {!isMember && (
              <span className="inline-flex px-2.5 py-0.5 rounded-xl text-xs font-medium bg-amber-500/10 text-amber-600 dark:text-amber-400 shrink-0">
                Not Connected
              </span>
            )}
            {channel?.member_count != null && (
              <span className="text-sm text-muted-foreground hidden sm:inline">
                {channel.member_count.toLocaleString()} members
              </span>
            )}
            <div className="ml-auto flex items-center gap-2 shrink-0">
              {id && <NextSyncBadge channelId={id} />}
              {id && <SyncButton syncState={syncState} isSyncing={isSyncing} error={syncError} onSync={triggerSync} />}
            </div>
          </div>
          {isMember && (
            <>
              {displayFailureMessage && (
                <div
                  className={cn(
                    "rounded-lg border px-3 py-2 text-xs",
                    isCoolingDown
                      ? "border-amber-200 dark:border-amber-900 bg-amber-50 dark:bg-amber-950/30 text-amber-700 dark:text-amber-300"
                      : "border-rose-200 dark:border-rose-900 bg-rose-50 dark:bg-rose-950/30 text-rose-700 dark:text-rose-300",
                  )}
                >
                  {isCoolingDown ? displayFailureMessage : `Sync failed: ${displayFailureMessage}`}
                </div>
              )}
              {syncCompletedWithNoNew && (
                <div className="rounded-lg border border-sky-200 dark:border-sky-900 bg-sky-50 dark:bg-sky-950/30 px-3 py-2 text-xs text-sky-700 dark:text-sky-300">
                  Sync completed. No new messages were found since the last sync.
                </div>
              )}
              <div className="sm:hidden">
                <label className="sr-only" htmlFor="channel-tab-select">
                  Select tab
                </label>
                <select
                  id="channel-tab-select"
                  value={activeTab}
                  onChange={(e) => handleTabChange(e.target.value)}
                  className="w-full h-9 px-3 rounded-lg border border-border bg-background text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/20"
                >
                  {TAB_PATHS.map((tab) => (
                    <option key={tab} value={tab}>
                      {TAB_LABELS[tab]}
                    </option>
                  ))}
                </select>
              </div>
              <div className="hidden sm:block overflow-x-auto no-scrollbar">
                <div className="flex items-center gap-2 min-w-max">
                  {/* Scope chip — anchors the tab strip to "this channel"
                      so operators don't have to read the breadcrumb to
                      remember what scope they're in. Hidden when the
                      channel hasn't loaded yet to avoid layout jump. */}
                  {channel && (
                    <div className="inline-flex items-center gap-1.5 rounded-full border border-border/60 bg-muted/30 pl-1.5 pr-2.5 py-1 text-xs font-medium text-foreground">
                      <span
                        aria-hidden
                        className={cn(
                          "h-2 w-2 rounded-full shrink-0",
                          PLATFORM_DOT[channel.platform] ?? "bg-slate-500",
                        )}
                        title={channel.platform}
                      />
                      <span className="truncate max-w-[180px]">
                        #{channelDisplayName}
                      </span>
                    </div>
                  )}
                  <span aria-hidden className="h-5 w-px bg-border/60 mx-0.5" />
                  {/* Tabs grouped by purpose — AI · raw · ops. */}
                  {TAB_GROUPS.map((group, groupIdx) => (
                    <div key={groupIdx} className="flex items-center gap-1">
                      {group.map((tab) => {
                        const Icon = TAB_ICONS[tab];
                        return (
                          <button
                            key={tab}
                            onClick={() => handleTabChange(tab)}
                            className={cn(
                              "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors",
                              activeTab === tab
                                ? "bg-primary/10 text-primary"
                                : "text-muted-foreground hover:text-foreground hover:bg-muted",
                            )}
                          >
                            <Icon className="h-3.5 w-3.5 shrink-0" />
                            {TAB_LABELS[tab]}
                          </button>
                        );
                      })}
                      {groupIdx < TAB_GROUPS.length - 1 && (
                        <span aria-hidden className="h-4 w-px bg-border/50 mx-1" />
                      )}
                    </div>
                  ))}
                </div>
              </div>
            </>
          )}
        </div>
      </div>

      {/* Sync progress monitor placement:
       *  - Wiki tab + EXPANDED + active sync → fullscreen monitor;
       *    body content hidden so the monitor fills the page.
       *  - Wiki tab + COLLAPSED → compact strip; Outlet visible below.
       *  - Non-wiki tabs → always compact; Outlet visible.
       *  No active sync → no monitor; Outlet has full content area.
       *
       *  Note: ``pipelineActive`` also serves as a connectivity proof —
       *  an in-flight sync implies the bot IS in the channel, so we
       *  must NOT render "Channel Not Connected" while pipeline runs
       *  even if ``isMember`` is still false (stale or unsynced). */}
      {(() => {
        if (!id || !pipelineActive) return null;
        const fullscreen = activeTab === "wiki" && !monitorCollapsed;
        if (fullscreen) {
          return (
            <div className="flex-1 min-h-0 flex flex-col px-4 sm:px-6 pt-3 pb-3">
              <SyncProgress
                syncState={syncState}
                isSyncing={isSyncing}
                channelId={id}
                collapsed={monitorCollapsed}
                onCollapsedChange={setMonitorCollapsed}
              />
            </div>
          );
        }
        return (
          <div className="shrink-0">
            <SyncProgress
              syncState={syncState}
              isSyncing={isSyncing}
              channelId={id}
              collapsed={monitorCollapsed}
              onCollapsedChange={setMonitorCollapsed}
            />
          </div>
        );
      })()}

      {/* Content area decision tree:
       *  1) Channel still loading → loading spinner
       *  2) Fullscreen monitor active → render nothing (monitor fills)
       *  3) Member OR pipeline active → Outlet (a running sync proves
       *     the channel is connected, so don't show the empty state)
       *  4) Otherwise → "Channel Not Connected" hero */}
      {loadingChannel ? (
        <div className="flex items-center justify-center flex-1 min-h-0 p-6">
          <div className="flex flex-col items-center gap-3 text-muted-foreground/50">
            <RefreshCw className="w-6 h-6 animate-spin" />
            <span className="text-sm">Loading channel...</span>
          </div>
        </div>
      ) : (activeTab === "wiki" && !monitorCollapsed && pipelineActive) ? null : (isMember || pipelineActive) ? (
        <div className="flex-1 min-h-0 relative bg-muted/10 overflow-hidden" key={activeTab}>
          <Outlet
            context={{
              syncState,
              isSyncing,
              triggerSync,
              syncError,
              connectionId: channel?.connection_id ?? null,
            }}
          />
          {/* Floating Ask button — icon FAB that expands on hover */}
          <button
            onClick={() => navigate(`/ask?context=${id}`)}
            aria-label="Ask about this channel"
            className="group absolute bottom-6 right-6 z-10 h-12 w-12 hover:w-56 focus-visible:w-56
                       flex items-center rounded-full overflow-hidden
                       bg-primary text-primary-foreground
                       shadow-[0_10px_30px_-10px_hsl(var(--primary)/0.55),0_4px_12px_-4px_rgba(0,0,0,0.3)]
                       ring-1 ring-primary/30
                       hover:-translate-y-0.5
                       hover:shadow-[0_18px_40px_-12px_hsl(var(--primary)/0.7),0_6px_18px_-4px_rgba(0,0,0,0.35)]
                       active:translate-y-0
                       focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 focus-visible:ring-offset-background
                       transition-[width,transform,box-shadow] duration-[400ms] ease-[cubic-bezier(0.22,1,0.36,1)]
                       will-change-[width,transform]"
          >
            <span className="flex h-12 w-12 shrink-0 items-center justify-center">
              <MessageCircleQuestion className="w-5 h-5 transition-transform duration-300 group-hover:scale-110 group-hover:-rotate-6" />
            </span>
            <span className="whitespace-nowrap pr-5 text-sm font-medium tracking-tight
                             opacity-0 -translate-x-1 group-hover:opacity-100 group-hover:translate-x-0
                             transition-[opacity,transform] duration-300 ease-out delay-75">
              Ask about this channel
            </span>
          </button>
        </div>
      ) : (
        <div className="flex items-center justify-center flex-1 min-h-0 p-6">
          <div className="max-w-lg w-full motion-safe:animate-rise-in">
            {/* Hero section */}
            <div className="bg-card border border-border rounded-2xl overflow-hidden">
              <div className="bg-gradient-to-br from-amber-500/5 via-orange-500/5 to-transparent px-8 pt-10 pb-6 text-center">
                <div className="mx-auto w-14 h-14 rounded-2xl bg-amber-500/10 border border-amber-500/20 flex items-center justify-center mb-5">
                  <ShieldAlert className="w-7 h-7 text-amber-500" />
                </div>
                <h3 className="text-xl font-semibold text-foreground mb-2">Channel Not Connected</h3>
                <p className="text-sm text-muted-foreground leading-relaxed max-w-sm mx-auto">
                  Add <span className="font-medium text-foreground">{instructions.botName}</span> to{" "}
                  <span className="font-medium text-foreground">#{channelDisplayName}</span> to start
                  building knowledge from its conversations.
                </p>
              </div>

              {/* Steps */}
              <div className="px-8 py-6 space-y-4">
                <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  How to connect
                </p>
                <div className="space-y-3">
                  {instructions.steps.map((step, i) => (
                    <div
                      key={i}
                      className="flex gap-3.5 items-start p-3 rounded-xl bg-muted/40 hover:bg-muted/70 transition-colors"
                    >
                      <span className="flex items-center justify-center w-6 h-6 rounded-lg bg-primary/10 text-primary text-xs font-bold shrink-0">
                        {i + 1}
                      </span>
                      <span className="text-sm text-foreground/80 leading-relaxed pt-0.5">{step}</span>
                    </div>
                  ))}
                </div>
              </div>

              {/* Action */}
              <div className="px-8 pb-8 pt-2">
                <button
                  onClick={handleRefreshStatus}
                  disabled={refreshing}
                  className="w-full inline-flex items-center justify-center gap-2 px-5 py-3 rounded-xl bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50"
                >
                  <RefreshCw className={cn("w-4 h-4", refreshing && "animate-spin")} />
                  {refreshing ? "Checking connection..." : "Refresh Status"}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * Index route redirect — always land on wiki; WikiTab handles its own empty state.
 */
export function ChannelDefaultRedirect() {
  return <Navigate to="wiki" replace />;
}
