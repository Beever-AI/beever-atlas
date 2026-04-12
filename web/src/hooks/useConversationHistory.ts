import { useState, useCallback, useEffect } from "react";

const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8000";

export interface ConversationSession {
  session_id: string;
  title: string | null;
  first_question: string;
  created_at: string;
  pinned: boolean;
}

export interface SessionMessage {
  role: "user" | "assistant";
  content: string;
  citations?: any[];
  tools_used?: string[];
  timestamp: string;
}

export function useConversationHistory(channelId: string) {
  const [sessions, setSessions] = useState<ConversationSession[]>([]);
  const [loading, setLoading] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");

  const fetchSessions = useCallback(async (search?: string) => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (search) params.set("search", search);
      const res = await fetch(`${API_BASE}/api/channels/${channelId}/ask/history?${params}`);
      if (res.ok) {
        const data = await res.json();
        setSessions(data.sessions ?? []);
      }
    } catch (err) {
      console.error("Failed to fetch sessions", err);
    } finally {
      setLoading(false);
    }
  }, [channelId]);

  const loadSession = useCallback(async (sessionId: string): Promise<SessionMessage[]> => {
    try {
      const res = await fetch(`${API_BASE}/api/channels/${channelId}/ask/sessions/${sessionId}`);
      if (res.ok) {
        const data = await res.json();
        return data.messages ?? [];
      }
    } catch (err) {
      console.error("Failed to load session", err);
    }
    return [];
  }, [channelId]);

  const renameSession = useCallback(async (sessionId: string, title: string) => {
    try {
      await fetch(`${API_BASE}/api/channels/${channelId}/ask/sessions/${sessionId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title }),
      });
      setSessions(prev => prev.map(s => s.session_id === sessionId ? { ...s, title } : s));
    } catch (err) {
      console.error("Failed to rename session", err);
    }
  }, [channelId]);

  const pinSession = useCallback(async (sessionId: string, pinned: boolean) => {
    try {
      await fetch(`${API_BASE}/api/channels/${channelId}/ask/sessions/${sessionId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pinned }),
      });
      setSessions(prev => prev.map(s => s.session_id === sessionId ? { ...s, pinned } : s));
    } catch (err) {
      console.error("Failed to pin session", err);
    }
  }, [channelId]);

  const deleteSession = useCallback(async (sessionId: string) => {
    try {
      await fetch(`${API_BASE}/api/channels/${channelId}/ask/sessions/${sessionId}`, {
        method: "DELETE",
      });
      setSessions(prev => prev.filter(s => s.session_id !== sessionId));
    } catch (err) {
      console.error("Failed to delete session", err);
    }
  }, [channelId]);

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
