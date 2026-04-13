import { useEffect, useState, useCallback } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { Skeleton } from "@/components/ui/skeleton";
import { Search } from "lucide-react";
import { useTheme } from "@/hooks/useTheme";
import { getPlatformBadgeStyle } from "@/lib/platform-badge";
import { StatCards } from "@/components/dashboard/StatCards";
import { ActivityFeed } from "@/components/dashboard/ActivityFeed";
import { useStats, useActivity } from "@/hooks/useStats";
import { WelcomeScreen } from "@/components/onboarding/WelcomeScreen";
import { ConnectionWizard } from "@/components/settings/ConnectionWizard";
import { useUserProfile } from "@/hooks/useUserProfile";
import type { PlatformConnection } from "@/lib/types";

type Platform = "slack" | "discord" | "teams" | "telegram";

interface Channel {
  channel_id: string;
  name: string;
  platform: string;
  is_member: boolean;
  member_count: number | null;
  topic: string | null;
  purpose: string | null;
}

export function Dashboard() {
  const navigate = useNavigate();
  const [channels, setChannels] = useState<Channel[]>([]);
  const [loading, setLoading] = useState(true);
  const [connections, setConnections] = useState<PlatformConnection[]>([]);
  const [connectionsLoading, setConnectionsLoading] = useState(true);
  const [showWizard, setShowWizard] = useState(false);
  const [wizardPlatform, setWizardPlatform] = useState<Platform>("slack");
  const { resolvedTheme } = useTheme();
  const isDark = resolvedTheme === "dark";
  const { stats, loading: statsLoading } = useStats();
  const { events, loading: activityLoading } = useActivity(5);
  const { profile, getGreeting } = useUserProfile();
  const firstName = profile.displayName ? profile.displayName.split(" ")[0] : "there";
  const greeting = getGreeting();

  const fetchConnections = useCallback(() => {
    setConnectionsLoading(true);
    api
      .get<PlatformConnection[]>("/api/connections")
      .then(setConnections)
      .catch(() => setConnections([]))
      .finally(() => setConnectionsLoading(false));
  }, []);

  useEffect(() => {
    fetchConnections();
  }, [fetchConnections]);

  useEffect(() => {
    api
      .get<Channel[]>("/api/channels")
      .then(setChannels)
      .catch(() => setChannels([]))
      .finally(() => setLoading(false));
  }, []);

  if (!connectionsLoading && connections.length === 0) {
    return (
      <>
        <WelcomeScreen
          onConnect={(platform) => {
            setWizardPlatform(platform as Platform);
            setShowWizard(true);
          }}
        />
        {showWizard && (
          <ConnectionWizard
            platform={wizardPlatform}
            onClose={() => setShowWizard(false)}
            onComplete={() => {
              setShowWizard(false);
              fetchConnections();
              window.dispatchEvent(new Event("connections-changed"));
            }}
          />
        )}
      </>
    );
  }

  return (
    <div className="h-full overflow-auto">
      <div className="max-w-[1400px] mx-auto p-6 sm:p-8 lg:p-12">
        {/* Hero Section */}
        <section className="flex flex-col items-center gap-5 py-12">
          <h1 className="font-heading text-[32px] tracking-tight text-foreground">
            {greeting}, {firstName}
          </h1>
          <p className="text-muted-foreground text-base">
            What would you like to know today?
          </p>

          {/* Ask bar */}
          <Link
            to="/ask?new=1"
            className="w-full max-w-4xl flex items-center gap-3 px-5 py-4 bg-card rounded-3xl border border-border shadow-sm hover:border-primary/30 transition-colors cursor-pointer"
          >
            <Search className="w-5 h-5 text-muted-foreground/60" />
            <span className="text-muted-foreground/60 text-base">
              Ask anything across all channels...
            </span>
            <div className="flex-1" />
            <kbd className="px-2 py-0.5 bg-muted rounded-md border border-border text-sm text-muted-foreground font-medium">
              ⌘K
            </kbd>
          </Link>

          {/* Suggestion pills */}
          <div className="flex gap-2 flex-wrap justify-center">
            {[
              "What decisions were made this week?",
              "Summarize #engineering",
              "Who owns the auth module?",
            ].map((q) => (
              <button
                key={q}
                onClick={() => navigate(`/ask?q=${encodeURIComponent(q)}`)}
                className="px-4 py-1.5 rounded-full bg-card border border-border text-sm text-muted-foreground hover:bg-muted transition-colors"
              >
                {q}
              </button>
            ))}
          </div>
        </section>

        {/* Stats */}
        <section className="mt-8">
          <StatCards stats={stats} loading={statsLoading} />
        </section>

        {/* Activity Feed */}
        <section className="mt-6">
          <ActivityFeed events={events} loading={activityLoading} />
          {events.length > 0 && (
            <div className="mt-2 text-center">
              <Link
                to="/activity"
                className="text-sm font-medium text-primary hover:text-primary/80"
              >
                View all activity →
              </Link>
            </div>
          )}
        </section>

        {/* Connected Channels */}
        <section className="mt-8">
          <div className="flex items-center justify-between mb-4">
            <h2 className="font-heading text-xl tracking-tight text-foreground">
              Connected Channels
            </h2>
            <Link
              to="/channels"
              className="text-sm font-medium text-primary hover:text-primary/80"
            >
              View all →
            </Link>
          </div>

          {(() => {
            const connected = channels.filter((ch) => ch.is_member);
            if (loading) {
              return (
                <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
                  {Array.from({ length: 3 }).map((_, i) => (
                    <div
                      key={i}
                      className="bg-card rounded-2xl border border-border p-5 flex flex-col gap-3"
                    >
                      <Skeleton className="h-5 w-32" />
                      <Skeleton className="h-4 w-16" />
                      <Skeleton className="h-3 w-full" />
                      <Skeleton className="h-3 w-3/4" />
                      <Skeleton className="h-3 w-24" />
                    </div>
                  ))}
                </div>
              );
            }
            if (connected.length === 0) {
              return (
                <div className="bg-card rounded-2xl border border-dashed border-border p-10 flex flex-col items-center gap-3 text-center">
                  <p className="text-sm font-medium text-foreground">No connected channels yet</p>
                  <p className="text-[15px] text-muted-foreground">
                    Add @beever to your Slack, Teams, or Discord channels to start building knowledge.
                  </p>
                  <Link
                    to="/channels"
                    className="mt-2 inline-flex items-center justify-center px-4 py-2 text-sm font-medium rounded-full border border-border bg-background hover:bg-muted transition-colors"
                  >
                    View all channels
                  </Link>
                </div>
              );
            }
            return (
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
                {connected
                  .sort((a, b) => (b.member_count ?? 0) - (a.member_count ?? 0))
                  .slice(0, 3)
                  .map((ch, idx) => (
                  <Link
                    to={`/channels/${ch.channel_id}/wiki`}
                    key={ch.channel_id}
                    state={{
                      channel_name: ch.name,
                      platform: ch.platform,
                      is_member: ch.is_member,
                      member_count: ch.member_count,
                    }}
                    className="bg-card rounded-2xl border border-border p-5 flex flex-col gap-3 hover:shadow-sm transition-shadow motion-safe:animate-rise-in"
                    style={{ animationDelay: `${idx * 55}ms` }}
                  >
                    <div className="flex items-center gap-2">
                      <span className="w-2 h-2 rounded-full shrink-0 bg-emerald-500" />
                      <span className="text-lg font-semibold text-primary">#</span>
                      <span className="text-base font-medium text-foreground truncate">
                        {ch.name}
                      </span>
                    </div>
                    <span
                      className="inline-flex w-fit px-2.5 py-0.5 rounded-xl text-xs font-medium capitalize"
                      style={getPlatformBadgeStyle(ch.platform, isDark)}
                    >
                      {ch.platform}
                    </span>
                    <p className="text-sm text-muted-foreground leading-relaxed line-clamp-2">
                      {ch.topic || ch.purpose || "No description"}
                    </p>
                    {ch.member_count != null && (
                      <div className="flex items-center gap-3 text-sm text-muted-foreground/70">
                        <span>{ch.member_count} members</span>
                      </div>
                    )}
                  </Link>
                ))}
              </div>
            );
          })()}
        </section>
      </div>
    </div>
  );
}
