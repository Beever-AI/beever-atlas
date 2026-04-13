import { useEffect, useState } from "react";
import { useParams, Outlet, useNavigate, useLocation, Link, Navigate } from "react-router-dom";
import { api } from "@/lib/api";
import { ArrowLeft, ShieldAlert, RefreshCw, MessageCircleQuestion } from "lucide-react";
import { cn } from "@/lib/utils";
import { useConnectionMap } from "@/hooks/useConnectionMap";
import { ChannelBreadcrumb } from "@/components/channel/Breadcrumb";
import { SyncButton } from "@/components/channel/SyncButton";
import { SyncProgress } from "@/components/channel/SyncProgress";
import { NextSyncBadge } from "@/components/channel/NextSyncBadge";
import { useSync } from "@/hooks/useSync";
import { LanguageBadge } from "@/components/channel/LanguageBadge";

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

const TAB_PATHS = ["wiki", "messages", "memories", "graph", "sync-history", "settings"] as const;
type TabPath = (typeof TAB_PATHS)[number];

const TAB_LABELS: Record<TabPath, string> = {
  wiki: "Wiki",
  messages: "Messages",
  memories: "Memories",
  graph: "Graph",
  "sync-history": "Sync History",
  settings: "Settings",
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
  const { getWorkspaceName } = useConnectionMap();

  const activeTab = getCurrentTab(location.pathname);

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
  const syncFailureMessage =
    syncError || (syncState.state === "error" ? syncState.errors?.filter(Boolean).join("; ") : null);
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
              {syncFailureMessage && (
                <div className="rounded-lg border border-rose-200 dark:border-rose-900 bg-rose-50 dark:bg-rose-950/30 px-3 py-2 text-xs text-rose-700 dark:text-rose-300">
                  Sync failed: {syncFailureMessage}
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
                <div className="flex gap-1 min-w-max">
                  {TAB_PATHS.map((tab) => (
                    <button
                      key={tab}
                      onClick={() => handleTabChange(tab)}
                      className={cn(
                        "px-3 py-1.5 rounded-lg text-sm font-medium transition-colors",
                        activeTab === tab
                          ? "bg-primary/10 text-primary"
                          : "text-muted-foreground hover:text-foreground hover:bg-muted",
                      )}
                    >
                      {TAB_LABELS[tab]}
                    </button>
                  ))}
                </div>
              </div>
            </>
          )}
        </div>
      </div>

      {/* Sync progress bar — always visible when syncing */}
      <div className="shrink-0">{id && <SyncProgress syncState={syncState} isSyncing={isSyncing} />}</div>

      {/* Content */}
      {loadingChannel ? (
        <div className="flex items-center justify-center flex-1 min-h-0 p-6">
          <div className="flex flex-col items-center gap-3 text-muted-foreground/50">
            <RefreshCw className="w-6 h-6 animate-spin" />
            <span className="text-sm">Loading channel...</span>
          </div>
        </div>
      ) : isMember ? (
        <div className="flex-1 min-h-0 relative bg-muted/10 overflow-hidden" key={activeTab}>
          <Outlet context={{ syncState, isSyncing, connectionId: channel?.connection_id ?? null }} />
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
