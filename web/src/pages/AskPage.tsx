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
    newChatNonce,
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
    // If the user just clicked "+ New chat" from a /ask/:id URL, navigate
    // fires before the URL propagates here. In that intermediate render
    // `paramSessionId` is still the old id and `activeSessionId` is null
    // (from newConversation). Without this guard the effect would re-assert
    // the old session, causing AskCorePicker to reload its messages and the
    // page to appear stuck on the previous chat.
    if (newChatIntent) return;
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
    newChatIntent,
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

  // Session-switch loading state. When the URL carries a :sessionId we did not
  // just mint in this tab and loadStatus hasn't settled yet, overlay a neutral
  // skeleton on top of AskCore so the user doesn't see a brief "new chat"
  // flash before the conversation's messages paint. AskCore still mounts
  // underneath so its picker fires loadSession and advances loadStatus.
  // Hooks must run before any conditional early return below.
  const rawSessionSwitchLoading =
    !!paramSessionId &&
    paramSessionId !== mintedSessionId &&
    (activeSessionId !== paramSessionId || loadStatus === "idle");
  // Hold the overlay visible for a short tail after the conditions above
  // clear. loadStatus flips to "ok" inside the context's loadSession (before
  // AskCore's .then callback commits the fetched messages to its local
  // state) — without the tail, the overlay vanishes one render before the
  // messages paint and the user sees the empty-composer flash.
  const [tailActive, setTailActive] = useState(false);
  useEffect(() => {
    if (rawSessionSwitchLoading) {
      setTailActive(false);
      return;
    }
    setTailActive(true);
    const t = setTimeout(() => setTailActive(false), 120);
    return () => clearTimeout(t);
  }, [rawSessionSwitchLoading]);
  const sessionSwitchLoading = rawSessionSwitchLoading || tailActive;

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
              // `?new=1` suppresses the bare-/ask redirect-to-latest, so
              // the user lands on a genuinely fresh composer.
              navigate("/ask?new=1");
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
    <div className="relative h-full">
      {sessionSwitchLoading && <ConversationSkeleton />}
      <AskCore
      // Remount when a fresh `?q=` or `?new=1` arrives so AskCorePicker's
      // internal `initialQuerySent` / `sessionIdRef` reset and the fresh
      // session is used instead of appending to the previous one.
      // `?q=` implies new-chat intent, so `q:` takes precedence over `new`.
      // Also remount on paramSessionId change so a /ask/:a → /ask/:b nav
      // reliably resets state.
      // Remount rules (ordered):
      //   1. `?q=foo` — fresh chat with prefilled query (remount per query)
      //   2. Navigating to a different persisted session (not the one we just
      //      minted in this tab) — remount with that session's id
      //   3. Otherwise — stable key that only changes when `newChatNonce`
      //      bumps. This preserves the AskCorePicker across:
      //      - `/ask?new=1` → `/ask/:id` mint-replace (streaming must not die)
      //      - user follow-ups on the same session
      //      …while still remounting on every explicit "+ New chat" click.
      key={
        initialQuery
          ? `q:${initialQuery}:${newChatNonce}`
          : paramSessionId && paramSessionId !== mintedSessionId
            ? `s:${paramSessionId}`
            : `n:${newChatNonce}`
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
    </div>
  );
}

function ConversationSkeleton() {
  // Shimmer palette matches the chat surface so the skeleton reads as "the
  // same conversation is loading" rather than a different empty page.
  const bubble =
    "rounded-2xl bg-muted/60 motion-safe:animate-pulse";
  const line = "h-3 rounded bg-muted/70 motion-safe:animate-pulse";
  return (
    <div
      className="absolute inset-0 z-10 bg-background flex flex-col"
      aria-busy="true"
      aria-label="Loading conversation"
      role="status"
    >
      <div className="flex-1 overflow-hidden">
        <div className="mx-auto w-full max-w-3xl px-4 sm:px-6 py-6 space-y-6">
          {/* User bubble (right) */}
          <div className="flex justify-end">
            <div className={`${bubble} px-4 py-3 w-[55%] max-w-[420px]`}>
              <div className={`${line} w-[80%] mb-2`} />
              <div className={`${line} w-[45%]`} />
            </div>
          </div>

          {/* Assistant bubble (left, wider) */}
          <div className="flex justify-start">
            <div className={`${bubble} px-4 py-3 w-[80%] max-w-[640px]`}>
              <div className={`${line} w-[95%] mb-2`} />
              <div className={`${line} w-[88%] mb-2`} />
              <div className={`${line} w-[72%] mb-2`} />
              <div className={`${line} w-[60%]`} />
            </div>
          </div>

          {/* User bubble (right) */}
          <div className="flex justify-end">
            <div className={`${bubble} px-4 py-3 w-[40%] max-w-[320px]`}>
              <div className={`${line} w-[70%]`} />
            </div>
          </div>

          {/* Assistant bubble (left) */}
          <div className="flex justify-start">
            <div className={`${bubble} px-4 py-3 w-[70%] max-w-[560px]`}>
              <div className={`${line} w-[90%] mb-2`} />
              <div className={`${line} w-[65%]`} />
            </div>
          </div>
        </div>
      </div>

      {/* Composer placeholder at the bottom matches AskCore's input bar. */}
      <div className="border-t border-border/60 px-4 sm:px-6 py-4">
        <div className="mx-auto w-full max-w-3xl">
          <div className="h-11 rounded-full bg-muted/50 motion-safe:animate-pulse" />
        </div>
      </div>

      <span className="sr-only">Loading conversation…</span>
    </div>
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
