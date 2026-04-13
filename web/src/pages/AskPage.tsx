import { useState, useEffect } from "react";
import { useSearchParams, Link } from "react-router-dom";
import { api } from "@/lib/api";
import { AskCore } from "@/components/channel/AskCore";
import type { ChannelOption } from "@/components/ask/ChannelPicker";
import { useAskSessions } from "@/contexts/AskSessionsContext";

interface ApiChannel {
  channel_id: string;
  name: string;
  platform: string;
  is_member: boolean;
}

/**
 * Ask page — conversation-first. The global Header already renders the page
 * title, so the page itself is pure chat surface. Empty state and composer
 * are owned by AskCore / ChatMessageList.
 */
export function AskPage() {
  const [searchParams] = useSearchParams();
  const contextChannelId = searchParams.get("context") ?? "";
  const initialQuery = searchParams.get("q") ?? "";
  const newChatIntent = searchParams.get("new") === "1";

  const [channels, setChannels] = useState<ChannelOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [initialChannelId, setInitialChannelId] = useState(contextChannelId);
  const { setActive, newConversation } = useAskSessions();

  useEffect(() => {
    setActive(true);
    return () => setActive(false);
  }, [setActive]);

  // Clear the sticky activeSessionId on any explicit "new chat" intent:
  //   - `?q=...` (Dashboard suggestion chips / pre-filled queries)
  //   - `?new=1` (Dashboard search bar — empty click = start fresh chat)
  // Without this, AskCore's session-load effect rehydrates the previous
  // conversation and (for ?q=) races the auto-send so the question is lost.
  // `newConversation` is stable via useCallback in AskSessionsContext, so
  // including it in deps is a no-op in practice but keeps the lint rule honest.
  useEffect(() => {
    if (initialQuery || newChatIntent) newConversation();
  }, [initialQuery, newChatIntent, newConversation]);

  useEffect(() => {
    api
      .get<ApiChannel[]>("/api/channels")
      .then((data) => {
        const connected = data
          .filter((ch) => ch.is_member)
          .map((ch) => ({
            channel_id: ch.channel_id,
            name: ch.name,
            platform: ch.platform,
          }));
        setChannels(connected);

        if (contextChannelId && connected.some((c) => c.channel_id === contextChannelId)) {
          setInitialChannelId(contextChannelId);
        } else if (connected.length > 0 && !initialChannelId) {
          setInitialChannelId(connected[0].channel_id);
        }
      })
      .catch(() => setChannels([]))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [contextChannelId]);

  if (loading) {
    return (
      <div className="h-full flex items-center justify-center text-muted-foreground/60 text-sm">
        Loading channels...
      </div>
    );
  }

  if (channels.length === 0) {
    return <NoChannelsState />;
  }

  return (
    <AskCore
      // Remount when a fresh `?q=` or `?new=1` arrives so AskCorePicker's
      // internal `initialQuerySent` / `sessionIdRef` reset and the fresh
      // session is used instead of appending to the previous one.
      // `?q=` implies new-chat intent, so `q:` takes precedence over `new`.
      key={initialQuery ? `q:${initialQuery}` : newChatIntent ? "new" : "idle"}
      channelMode="picker"
      channelId={initialChannelId || channels[0].channel_id}
      initialQuery={initialQuery || undefined}
      availableChannels={channels}
    />
  );
}

function NoChannelsState() {
  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-[1400px] mx-auto px-6 sm:p-8 lg:p-12 pt-16 pb-12 motion-safe:animate-rise-in">
        <section className="flex flex-col items-center gap-4 py-12 text-center">
          <h1 className="font-heading text-[32px] tracking-tight text-foreground">
            No channels connected yet
          </h1>
          <p className="text-muted-foreground text-base max-w-xl">
            Beever turns conversations in Slack, Teams, and Discord into answerable
            knowledge. Connect a channel to start asking questions.
          </p>
          <Link
            to="/channels"
            className="mt-4 inline-flex items-center justify-center h-10 px-5 rounded-full text-sm font-medium border border-border bg-card hover:bg-muted transition-colors text-foreground"
          >
            Connect a channel →
          </Link>
        </section>
      </div>
    </div>
  );
}
