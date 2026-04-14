import { createContext, useContext, useState, useCallback, useMemo } from "react";
import { useGlobalConversationHistory } from "@/hooks/useGlobalConversationHistory";
import type { GlobalConversationSession } from "@/hooks/useGlobalConversationHistory";
import type { Message } from "@/types/askTypes";

interface AskSessionsContextValue {
  /**
   * Whether the Ask page is active. Controlled by AskPage mounting — when
   * AskPage mounts it calls `setActive(true)` and on unmount it calls
   * `setActive(false)`. Sidebar uses this to swap channel list ↔ conversations.
   */
  isActive: boolean;
  setActive: (active: boolean) => void;

  /** Global (channel-less) session list for the current user */
  sessions: GlobalConversationSession[];
  /** Currently active session ID (selected in sidebar or loaded from URL) */
  activeSessionId: string | null;
  setActiveSessionId: (id: string | null) => void;

  /** Search query for filtering conversations in the sidebar */
  searchQuery: string;
  setSearchQuery: (q: string) => void;

  /** Infinite-scroll pagination */
  hasMore: boolean;
  loadingMore: boolean;
  loadMore: () => void;

  /** Session CRUD actions */
  fetchSessions: () => void;
  loadSession: (sessionId: string) => Promise<Message[]>;
  /** Last loadSession outcome — drives the "not-available" panel on /ask/:id. */
  loadStatus: "idle" | "ok" | "forbidden" | "not_found" | "error";
  clearLoadStatus: () => void;
  renameSession: (sessionId: string, title: string) => void;
  pinSession: (sessionId: string, pinned: boolean) => void;
  deleteSession: (sessionId: string) => void;
  /** Start a new conversation — clears activeSessionId */
  newConversation: () => void;
}

const AskSessionsContext = createContext<AskSessionsContextValue | null>(null);

export function useAskSessions(): AskSessionsContextValue {
  const ctx = useContext(AskSessionsContext);
  if (!ctx) {
    // No-op default when outside AskSessionsProvider — allows Sidebar to call
    // useAskSessions safely on all routes
    return {
      isActive: false,
      setActive: () => {},
      sessions: [],
      activeSessionId: null,
      setActiveSessionId: () => {},
      searchQuery: "",
      setSearchQuery: () => {},
      hasMore: false,
      loadingMore: false,
      loadMore: () => {},
      fetchSessions: () => {},
      loadSession: async () => [],
      loadStatus: "idle",
      clearLoadStatus: () => {},
      renameSession: () => {},
      pinSession: () => {},
      deleteSession: () => {},
      newConversation: () => {},
    };
  }
  return ctx;
}

export function AskSessionsProvider({ children }: { children: React.ReactNode }) {
  const [isActive, setActive] = useState(false);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [loadStatus, setLoadStatus] = useState<
    "idle" | "ok" | "forbidden" | "not_found" | "error"
  >("idle");

  const history = useGlobalConversationHistory();

  const clearLoadStatus = useCallback(() => setLoadStatus("idle"), []);

  const loadSession = useCallback(
    async (sessionId: string): Promise<Message[]> => {
      const { messages: sessionMessages, status } =
        await history.loadSession(sessionId);
      setLoadStatus(status);
      if (sessionMessages.length > 0) {
        const converted: Message[] = sessionMessages.map((m, i) => ({
          id: `loaded-${i}`,
          role: m.role,
          content: m.content,
          citations: m.citations ?? [],
          toolCalls: (m.tool_calls ?? []).map((tc) => ({
            tool_name: tc.tool_name,
            input: tc.input ?? {},
            result_summary: tc.result_summary,
            latency_ms: tc.latency_ms,
            facts_found: tc.facts_found,
            // Persisted calls are always terminal — anything still "running"
            // at save time was interrupted, so we surface it as done to avoid
            // a phantom spinner on rehydrated history.
            status: (tc.status === "error" ? "error" : "done") as
              | "running"
              | "done"
              | "error",
            started_at: 0,
          })),
          thinking: m.thinking?.text ? [m.thinking.text] : [],
          thinkingDuration: m.thinking?.duration_ms ?? null,
          metadata: null,
          isStreaming: false,
          channel_id: m.channel_id,
        }));
        setActiveSessionId(sessionId);
        return converted;
      }
      return [];
    },
    [history],
  );

  const newConversation = useCallback(() => {
    setActiveSessionId(null);
  }, []);

  const value = useMemo<AskSessionsContextValue>(
    () => ({
      isActive,
      setActive,
      sessions: history.sessions,
      activeSessionId,
      setActiveSessionId,
      searchQuery: history.searchQuery,
      setSearchQuery: history.setSearchQuery,
      hasMore: history.hasMore,
      loadingMore: history.loadingMore,
      loadMore: history.loadMore,
      fetchSessions: history.fetchSessions,
      loadSession,
      loadStatus,
      clearLoadStatus,
      renameSession: history.renameSession,
      pinSession: history.pinSession,
      deleteSession: history.deleteSession,
      newConversation,
    }),
    [
      isActive,
      history.sessions,
      activeSessionId,
      history.searchQuery,
      history.fetchSessions,
      history.setSearchQuery,
      history.hasMore,
      history.loadingMore,
      history.loadMore,
      loadSession,
      loadStatus,
      clearLoadStatus,
      history.renameSession,
      history.pinSession,
      history.deleteSession,
      newConversation,
    ],
  );

  return (
    <AskSessionsContext.Provider value={value}>
      {children}
    </AskSessionsContext.Provider>
  );
}
