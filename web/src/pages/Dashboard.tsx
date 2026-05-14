import { useEffect, useState, useCallback } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { BookOpen, Download, Search } from "lucide-react";
import { useWikiStates } from "@/hooks/useWikiStates";
import { useRecentChannels } from "@/hooks/useRecentChannels";
import { WikiBookCard } from "@/components/shared/WikiBookCard";
import { cn } from "@/lib/utils";
import { WelcomeScreen } from "@/components/onboarding/WelcomeScreen";
import { ConnectionWizard } from "@/components/settings/ConnectionWizard";
import { FileImportWizard } from "@/components/settings/FileImportWizard";
import { useUserProfile } from "@/hooks/useUserProfile";
import type { PlatformConnection } from "@/lib/types";

type Platform = "slack" | "discord" | "teams" | "telegram" | "mattermost";

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
  const [showFileImport, setShowFileImport] = useState(false);
  const { wikiStates, getState: getWikiState } = useWikiStates();
  const { recent: recentChannels } = useRecentChannels();
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
            if (platform === "file") {
              setShowFileImport(true);
            } else {
              setWizardPlatform(platform as Platform);
              setShowWizard(true);
            }
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
        {showFileImport && (
          <FileImportWizard
            onClose={() => setShowFileImport(false)}
            onComplete={() => {
              setShowFileImport(false);
              fetchConnections();
              window.dispatchEvent(new Event("connections-changed"));
            }}
          />
        )}
      </>
    );
  }

  // Build the (up to 3) cards for the home section.
  //
  // Find-tier (default): start with recently-visited wikis, then top up
  // the remaining slots with other wiki-ready channels the user hasn't
  // visited yet. This way one recent visit doesn't eclipse two other
  // ready wikis — the 3-up grid stays full whenever possible.
  //
  // Sync-tier (fallback): only triggers when nothing in find-tier
  // applies (no recents AND no ready channels). Surfaces up to 3
  // empty connected channels with cards that route to the channel
  // detail page (not /wiki) so the user lands on the sync controls.
  const SLOT_COUNT = 3;

  const fromRecents = recentChannels
    .slice(0, SLOT_COUNT)
    .map((r) => ({
      channel_id: r.channel_id,
      name: r.name,
      platform: r.platform,
      visited_at: r.visited_at as string | null,
    }));

  const recentIds = new Set(fromRecents.map((r) => r.channel_id));
  const remainingSlots = Math.max(0, SLOT_COUNT - fromRecents.length);

  const readyFill = remainingSlots > 0
    ? channels
        .filter(
          (ch) =>
            ch.is_member &&
            getWikiState(ch.channel_id) === "ready" &&
            !recentIds.has(ch.channel_id),
        )
        .slice(0, remainingSlots)
        .map((ch) => ({
          channel_id: ch.channel_id,
          name: ch.name,
          platform: ch.platform,
          visited_at: null as string | null,
        }))
    : [];

  const findCards = [...fromRecents, ...readyFill];

  const syncSeeds =
    findCards.length === 0
      ? channels
          .filter(
            (ch) =>
              ch.is_member &&
              (getWikiState(ch.channel_id) === "empty" ||
                getWikiState(ch.channel_id) === "errored"),
          )
          .slice(0, SLOT_COUNT)
          .map((ch) => ({
            channel_id: ch.channel_id,
            name: ch.name,
            platform: ch.platform,
            visited_at: null as string | null,
          }))
      : [];

  const wikiCards = findCards.length > 0 ? findCards : syncSeeds;
  const tier: "find" | "sync" = findCards.length > 0 ? "find" : "sync";

  return (
    <div className="h-full overflow-auto">
      <div className="max-w-[1200px] mx-auto p-6 sm:p-8 lg:p-12">
        {/* Hero — greeting, ask bar, suggestion pills. The launchpad. */}
        <section className="flex flex-col items-center gap-5 py-12">
          <h1 className="font-heading text-[32px] tracking-tight text-foreground">
            {greeting}, {firstName}
          </h1>
          <p className="text-muted-foreground text-base">
            What would you like to know today?
          </p>

          <Link
            to="/ask?new=1"
            className="w-full max-w-3xl flex items-center gap-3 px-5 py-4 bg-card rounded-3xl border border-border shadow-sm hover:border-primary/30 transition-colors cursor-pointer"
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

        {/* The home page's only content section. Two tiers:
            - "find": the user has wikis (visited or ready) → surface them.
            - "sync": connected channels exist but none are ingested →
              point the user at the empty channels with cards that route
              straight to the sync controls (channel detail page, not
              /wiki). Section header swaps icon + title so the intent is
              obvious before the user reads the cards. */}
        <section className="mt-10">
          <div className="flex items-center justify-between mb-5">
            <div className="flex items-center gap-2.5">
              <span
                className={cn(
                  "flex h-9 w-9 items-center justify-center rounded-xl",
                  tier === "find"
                    ? "bg-primary/10 text-primary"
                    : "bg-amber-500/10 text-amber-500 dark:text-amber-400",
                )}
              >
                {tier === "find" ? (
                  <BookOpen size={18} strokeWidth={2} />
                ) : (
                  <Download size={18} strokeWidth={2} />
                )}
              </span>
              <div className="flex flex-col">
                <h2 className="font-heading text-xl tracking-tight text-foreground">
                  {tier === "find"
                    ? "Find Your Channels Wiki"
                    : "Sync your channel now"}
                </h2>
                {tier === "sync" && (
                  <span className="text-xs text-muted-foreground/70">
                    Pick a channel to ingest it and build its wiki.
                  </span>
                )}
              </div>
            </div>
            <Link
              to="/channels"
              className="text-sm font-medium text-primary hover:text-primary/80"
            >
              View all →
            </Link>
          </div>

          {loading && wikiCards.length === 0 ? (
            <div className="bg-card rounded-2xl border border-dashed border-border p-8 text-center">
              <p className="text-sm text-muted-foreground">Loading your channels…</p>
            </div>
          ) : wikiCards.length === 0 ? (
            <div className="bg-card rounded-2xl border border-dashed border-border p-10 flex flex-col items-center gap-3 text-center">
              <BookOpen className="w-8 h-8 text-muted-foreground/40" strokeWidth={1.5} />
              <p className="text-sm font-medium text-foreground">No channels yet</p>
              <p className="text-[15px] text-muted-foreground max-w-md">
                Connect a channel to start. Once connected, Beever can ingest its messages and build a wiki you can ask about.
              </p>
              <Link
                to="/channels"
                className="mt-2 inline-flex items-center justify-center px-4 py-2 text-sm font-medium rounded-full border border-border bg-background hover:bg-muted transition-colors"
              >
                View all channels
              </Link>
            </div>
          ) : (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
              {wikiCards.map((card, idx) => {
                const state = getWikiState(card.channel_id);
                const entry = wikiStates[card.channel_id];
                return (
                  <WikiBookCard
                    key={card.channel_id}
                    channelId={card.channel_id}
                    name={card.name}
                    platform={card.platform}
                    state={state}
                    visitedAt={card.visited_at}
                    lastSyncTs={entry?.last_sync_ts ?? null}
                    // Sync tier routes to the channel page (sync controls),
                    // not the wiki tab — there's no wiki to land on yet.
                    to={tier === "sync" ? `/channels/${card.channel_id}` : undefined}
                    size="md"
                    animationDelayMs={idx * 55}
                  />
                );
              })}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
