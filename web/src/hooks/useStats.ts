import { useState, useEffect, useCallback } from "react";
import { api } from "@/lib/api";

export interface Stats {
  total_memories: number;
  total_entities: number;
  total_relationships: number;
  channels_synced: number;
  last_sync_at: string | null;
}

export interface ActivityEvent {
  id: string;
  event_type: "sync_complete" | "sync_failed" | "new_entity" | string;
  channel_id: string;
  details: Record<string, unknown>;
  timestamp: string;
}

export function useStats() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  const fetchStats = useCallback(() => {
    api
      .get<Stats>("/api/stats")
      .then((data) => {
        setStats(data);
        setError(null);
      })
      .catch((err: Error) => setError(err))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetchStats();
    const interval = setInterval(fetchStats, 30_000);
    return () => clearInterval(interval);
  }, [fetchStats]);

  return { stats, loading, error };
}

export function useActivity(limit = 20) {
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  const fetchActivity = useCallback(() => {
    api
      .get<ActivityEvent[]>(`/api/activity?limit=${limit}`)
      .then((data) => {
        setEvents(data);
        setError(null);
      })
      .catch((err: Error) => setError(err))
      .finally(() => setLoading(false));
  }, [limit]);

  useEffect(() => {
    fetchActivity();
    const interval = setInterval(fetchActivity, 30_000);
    return () => clearInterval(interval);
  }, [fetchActivity]);

  return { events, loading, error };
}

export interface BatchBreakdown {
  batch_num: number;
  facts_count: number;
  entities_count: number;
  relationships_count: number;
  sample_facts: string[];
  sample_entities: { name: string; type: string }[];
  sample_relationships: { source: string; target: string; type: string }[];
  duration_seconds: number;
}

export interface SyncHistoryEvent extends ActivityEvent {
  details: {
    job_id?: string;
    channel_name?: string;
    total_facts?: number;
    total_entities?: number;
    total_relationships?: number;
    total_messages?: number;
    error_count?: number;
    results_summary?: BatchBreakdown[];
    [key: string]: unknown;
  };
}

export function useSyncHistory(limit = 20) {
  const [entries, setEntries] = useState<SyncHistoryEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  const fetchHistory = useCallback(() => {
    api
      .get<SyncHistoryEvent[]>(`/api/sync-history?limit=${limit}`)
      .then((data) => {
        setEntries(data);
        setError(null);
      })
      .catch((err: Error) => setError(err))
      .finally(() => setLoading(false));
  }, [limit]);

  useEffect(() => {
    fetchHistory();
    const interval = setInterval(fetchHistory, 30_000);
    return () => clearInterval(interval);
  }, [fetchHistory]);

  return { entries, loading, error };
}
