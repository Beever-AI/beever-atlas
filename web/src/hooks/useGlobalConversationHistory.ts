import { useState, useCallback, useEffect } from "react";

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

  const fetchSessions = useCallback(async (search?: string) => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (search) params.set("search", search);
      const res = await fetch(`${API_BASE}/api/ask/sessions?${params}`);
      if (res.ok) {
        const data = await res.json();
        setSessions(data.sessions ?? []);
      }
    } catch (err) {
      console.error("Failed to fetch global sessions", err);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadSession = useCallback(
    async (sessionId: string): Promise<{ messages: SessionMessage[]; channel_ids: string[] }> => {
      try {
        const res = await fetch(`${API_BASE}/api/ask/sessions/${sessionId}`);
        if (res.ok) {
          const data = await res.json();
          return {
            messages: data.messages ?? [],
            channel_ids: data.channel_ids ?? [],
          };
        }
      } catch (err) {
        console.error("Failed to load session", err);
      }
      return { messages: [], channel_ids: [] };
    },
    [],
  );

  const renameSession = useCallback(async (sessionId: string, title: string) => {
    try {
      await fetch(`${API_BASE}/api/ask/sessions/${sessionId}`, {
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
      await fetch(`${API_BASE}/api/ask/sessions/${sessionId}`, {
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
      await fetch(`${API_BASE}/api/ask/sessions/${sessionId}`, {
        method: "DELETE",
      });
      setSessions((prev) => prev.filter((s) => s.session_id !== sessionId));
    } catch (err) {
      console.error("Failed to delete session", err);
    }
  }, []);

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
    fetchSessions,
    loadSession,
    renameSession,
    pinSession,
    deleteSession,
  };
}
