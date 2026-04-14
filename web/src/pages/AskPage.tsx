import { useState, useEffect } from "react";
import {
  useSearchParams,
  useParams,
  useNavigate,
  Link,
} from "react-router-dom";
import { api, authFetch } from "@/lib/api";
import { AskCore } from "@/components/channel/AskCore";
import type { ChannelOption } from "@/components/ask/ChannelPicker";
import { useAskSessions } from "@/contexts/AskSessionsContext";

const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8000";

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
  const { sessionId: paramSessionId } = useParams<{ sessionId?: string }>();
  const navigate = useNavigate();
  const contextChannelId = searchParams.get("context") ?? "";
  const initialQuery = searchParams.get("q") ?? "";
  const newChatIntent = searchParams.get("new") === "1";

  const [channels, setChannels] = useState<ChannelOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [initialChannelId, setInitialChannelId] = useState(contextChannelId);
  const {
    setActive,
    newConversation,
    setActiveSessionId,
    activeSessionId,
    loadStatus,
    clearLoadStatus,
  } = useAskSessions();

  // Bare-/ask redirect-to-latest state. `"pending"` = deciding; `"done"` = rendered.
  const [bareResolved, setBareResolved] = useState<"pending" | "done">(
    paramSessionId ? "done" : "pending",
  );
  // Track the session id that the active AskCore just minted. When
  // `paramSessionId === mintedSessionId`, the `/ask` → `/ask/:id` URL change is
  // the mint replace — AskCore already holds that conversation in memory, so
  // (a) the `key` must stay stable to avoid remounting (which would tear down
  // the live stream) and (b) the history-loader effect must skip the GET
  // (which would 404 before the session finishes persisting).
  const [mintedSessionId, setMintedSessionId] = useState<string | null>(null);
  // Foreign/unknown session-id state — derived from loadStatus. Redundant-fetch
  // guard: when the just-minted sessionId matches paramSessionId AND context
  // already has it as active (AskCore set it after stream metadata), the
  // loadSession effect in AskCorePicker is skipped (id === current), so no
  // extra GET fires — loadStatus stays `idle` for that id.
  const notAvailable =
    !!paramSessionId &&
    paramSessionId !== mintedSessionId &&
    (loadStatus === "forbidden" || loadStatus === "not_found");

  useEffect(() => {
    setActive(true);
    return () => setActive(false);
  }, [setActive]);

  // Clear the sticky activeSessionId on any explicit "new chat" intent:
  //   - `?q=...` (Dashboard suggestion chips / pre-filled queries)
  //   - `?new=1` (Dashboard search bar — empty click = start fresh chat)
  //   - `?context=<id>` (Channel workspace "Ask about this channel" FAB)
  useEffect(() => {
    if (initialQuery || newChatIntent || contextChannelId) newConversation();
  }, [initialQuery, newChatIntent, contextChannelId, newConversation]);

  // Bare-/ask: on mount with no :sessionId and no fresh-chat intent, fetch
  // latest session and redirect. If none exists, stay on bare /ask with an
  // empty composer (phase stays `idle`).
  useEffect(() => {
    if (paramSessionId) return;
    if (initialQuery || newChatIntent || contextChannelId) {
      // Explicit new-chat intent (query, new-chat flag, or "Ask about this
      // channel" from a channel workspace). Skip the redirect and let the user
      // start fresh with the preselected channel.
      setBareResolved("done");
      return;
    }
    let cancelled = false;
    authFetch(`${API_BASE}/api/ask/sessions?page=1&page_size=1`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (cancelled) return;
        const latest = data?.sessions?.[0]?.session_id;
        if (latest) {
          navigate(`/ask/${latest}`, { replace: true });
        } else {
          setBareResolved("done");
        }
      })
      .catch(() => {
        if (!cancelled) setBareResolved("done");
      });
    return () => {
      cancelled = true;
    };
    // Only re-run on route changes, not on query-string tweaks.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [paramSessionId]);

  // Reconcile paramSessionId with context. When the just-minted id matches
  // (mint-replace from bare /ask), skip the reconcile entirely — the running
  // AskCore instance already holds that conversation and firing `loadSession`
  // would race against server-side persistence and surface a spurious 404.
  // If the user later navigates to a different session, drop mintedSessionId
  // so the next mint on a fresh /ask gets its own tracking.
  useEffect(() => {
    if (!paramSessionId) return;
    if (paramSessionId === mintedSessionId) return;
    if (mintedSessionId && paramSessionId !== mintedSessionId) {
      setMintedSessionId(null);
    }
    if (activeSessionId === paramSessionId) return;
    clearLoadStatus();
    setActiveSessionId(paramSessionId);
  }, [
    paramSessionId,
    activeSessionId,
    mintedSessionId,
    setActiveSessionId,
    clearLoadStatus,
  ]);

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

  if (loading || (!paramSessionId && bareResolved === "pending")) {
    return (
      <div className="h-full flex items-center justify-center text-muted-foreground/60 text-sm">
        Loading channels...
      </div>
    );
  }

  if (channels.length === 0) {
    return <NoChannelsState />;
  }

  if (notAvailable) {
    return (
      <div
        className="h-full flex items-center justify-center p-8"
        data-testid="ask-not-available"
      >
        <div className="max-w-md text-center flex flex-col items-center gap-4">
          <h1 className="font-heading text-xl text-foreground">
            This conversation isn't available
          </h1>
          <p className="text-sm text-muted-foreground">
            It may have been deleted, or you may not have access to it.
          </p>
          <button
            onClick={() => {
              clearLoadStatus();
              newConversation();
              navigate("/ask");
            }}
            className="inline-flex items-center justify-center h-10 px-5 rounded-full text-sm font-medium border border-border bg-card hover:bg-muted transition-colors text-foreground"
          >
            Start a new chat
          </button>
        </div>
      </div>
    );
  }

  return (
    <AskCore
      // Remount when a fresh `?q=` or `?new=1` arrives so AskCorePicker's
      // internal `initialQuerySent` / `sessionIdRef` reset and the fresh
      // session is used instead of appending to the previous one.
      // `?q=` implies new-chat intent, so `q:` takes precedence over `new`.
      // Also remount on paramSessionId change so a /ask/:a → /ask/:b nav
      // reliably resets state.
      key={
        initialQuery
          ? `q:${initialQuery}`
          : newChatIntent
            ? "new"
            : paramSessionId && paramSessionId !== mintedSessionId
              ? `s:${paramSessionId}`
              : "idle"
      }
      channelMode="picker"
      channelId={initialChannelId || channels[0].channel_id}
      initialQuery={initialQuery || undefined}
      availableChannels={channels}
      urlSessionId={paramSessionId}
      onSessionMinted={(id) => {
        // creating → streaming: navigate(replace) so refresh is safe.
        // Record the minted id BEFORE navigating so the URL-sync effect and
        // key logic can recognize this transition as the mint-replace (same
        // conversation, no remount, no loader fetch).
        if (!paramSessionId) {
          setMintedSessionId(id);
          navigate(`/ask/${id}`, { replace: true });
        }
      }}
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
