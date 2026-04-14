import { useState, useCallback, useEffect, useRef } from "react";
import { authFetch } from "../lib/api";

const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8000";

export interface GlobalConversationSession {
  session_id: string;
  title: string | null;
  first_question: string;
  created_at: string;
  pinned: boolean;
  /** Distinct channels used in this session (derived at read time). */
  channel_ids: string[];
}

export interface PersistedThinking {
  text: string;
  duration_ms: number | null;
  truncated: boolean;
}

export interface PersistedToolCall {
  tool_name: string;
  input: Record<string, unknown>;
  status?: "running" | "done" | "error";
  result_summary?: string;
  latency_ms?: number;
  facts_found?: number;
}

export interface SessionMessage {
  role: "user" | "assistant";
  content: string;
  citations?: any[];
  tools_used?: string[];
  timestamp: string;
  /** Channel queried for this turn (v2). Falls back to session top-level for legacy. */
  channel_id?: string;
  thinking?: PersistedThinking | null;
  tool_calls?: PersistedToolCall[];
}

export interface SessionsPage {
  sessions: GlobalConversationSession[];
  page: number;
  page_size: number;
  has_more: boolean;
}

const PAGE_SIZE = 20;

/**
 * Channel-less conversation history. Hits /api/ask/sessions (v2 endpoints).
 *
 * Unlike useConversationHistory(channelId), this returns sessions across all
 * channels for the authenticated user. Each session exposes `channel_ids[]`
 * so the sidebar can render per-session channel badges.
 */
export function useGlobalConversationHistory() {
  const [sessions, setSessions] = useState<GlobalConversationSession[]>([]);
  const [loading, setLoading] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  // nextPage tracks the next page number to fetch when loading more.
  const nextPageRef = useRef(2);
  const loadingMoreRef = useRef(false);
  // Keep a ref to the current search query so loadMore can read it without
  // being in its dependency array (avoids recreating on every keystroke).
  const searchQueryRef = useRef(searchQuery);

  const fetchSessions = useCallback(async (search?: string) => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (search) params.set("search", search);
      params.set("page", "1");
      params.set("page_size", String(PAGE_SIZE));
      const res = await authFetch(`${API_BASE}/api/ask/sessions?${params}`);
      if (res.ok) {
        const data: SessionsPage = await res.json();
        setSessions(data.sessions ?? []);
        setHasMore(data.has_more ?? false);
        nextPageRef.current = 2;
      }
    } catch (err) {
      console.error("Failed to fetch global sessions", err);
    } finally {
      setLoading(false);
    }
  }, []);

  // Keep ref in sync so loadMore always reads the latest search query.
  searchQueryRef.current = searchQuery;

  const loadMore = useCallback(async () => {
    if (loadingMoreRef.current) return;
    loadingMoreRef.current = true;
    setLoadingMore(true);
    try {
      const params = new URLSearchParams();
      const search = searchQueryRef.current;
      if (search) params.set("search", search);
      params.set("page", String(nextPageRef.current));
      params.set("page_size", String(PAGE_SIZE));
      const res = await authFetch(`${API_BASE}/api/ask/sessions?${params}`);
      if (res.ok) {
        const data: SessionsPage = await res.json();
        setSessions((prev) => {
          const existingIds = new Set(prev.map((s) => s.session_id));
          const newItems = (data.sessions ?? []).filter(
            (s) => !existingIds.has(s.session_id),
          );
          return [...prev, ...newItems];
        });
        setHasMore(data.has_more ?? false);
        nextPageRef.current += 1;
      }
    } catch (err) {
      console.error("Failed to load more sessions", err);
    } finally {
      loadingMoreRef.current = false;
      setLoadingMore(false);
    }
  }, []);

  const loadSession = useCallback(
    async (
      sessionId: string,
    ): Promise<{
      messages: SessionMessage[];
      channel_ids: string[];
      status: "ok" | "forbidden" | "not_found" | "error";
    }> => {
      try {
        const res = await authFetch(`${API_BASE}/api/ask/sessions/${sessionId}`);
        if (res.ok) {
          const data = await res.json();
          return {
            messages: data.messages ?? [],
            channel_ids: data.channel_ids ?? [],
            status: "ok",
          };
        }
        if (res.status === 403) return { messages: [], channel_ids: [], status: "forbidden" };
        if (res.status === 404) return { messages: [], channel_ids: [], status: "not_found" };
      } catch (err) {
        console.error("Failed to load session", err);
      }
      return { messages: [], channel_ids: [], status: "error" };
    },
    [],
  );

  const renameSession = useCallback(async (sessionId: string, title: string) => {
    try {
      await authFetch(`${API_BASE}/api/ask/sessions/${sessionId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title }),
      });
      setSessions((prev) =>
        prev.map((s) => (s.session_id === sessionId ? { ...s, title } : s)),
      );
    } catch (err) {
      console.error("Failed to rename session", err);
    }
  }, []);

  const pinSession = useCallback(async (sessionId: string, pinned: boolean) => {
    try {
      await authFetch(`${API_BASE}/api/ask/sessions/${sessionId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pinned }),
      });
      setSessions((prev) =>
        prev.map((s) => (s.session_id === sessionId ? { ...s, pinned } : s)),
      );
    } catch (err) {
      console.error("Failed to pin session", err);
    }
  }, []);

  const deleteSession = useCallback(async (sessionId: string) => {
    try {
      const res = await authFetch(`${API_BASE}/api/ask/sessions/${sessionId}`, {
        method: "DELETE",
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        console.error("Failed to delete session", res.status, body);
        return;
      }
      // Optimistically remove locally so a racing stale fetch can't resurrect it.
      setSessions((prev) => prev.filter((s) => s.session_id !== sessionId));
      // Refetch page 1 and reset pagination.
      await fetchSessions(undefined);
    } catch (err) {
      console.error("Failed to delete session", err);
    }
  }, [fetchSessions]);

  useEffect(() => {
    const timer = setTimeout(() => {
      fetchSessions(searchQuery || undefined);
    }, 300);
    return () => clearTimeout(timer);
  }, [searchQuery, fetchSessions]);

  return {
    sessions,
    loading,
    searchQuery,
    setSearchQuery,
    hasMore,
    loadingMore,
    fetchSessions,
    loadMore,
    loadSession,
    renameSession,
    pinSession,
    deleteSession,
  };
}
