import { useState, useEffect, useCallback } from "react";
import { api } from "@/lib/api";
import type { PlatformConnection, PlatformCredentials, AvailableChannel } from "@/lib/types";

export interface UseConnectionsReturn {
  connections: PlatformConnection[];
  loading: boolean;
  error: string | null;
  refetch: () => void;
}

export function useConnections(): UseConnectionsReturn {
  const [connections, setConnections] = useState<PlatformConnection[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchConnections = useCallback(() => {
    setLoading(true);
    setError(null);
    api
      .get<PlatformConnection[]>("/api/connections")
      .then(setConnections)
      .catch((err) => {
        setError(err instanceof Error ? err.message : "Failed to load connections");
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetchConnections();
  }, [fetchConnections]);

  return { connections, loading, error, refetch: fetchConnections };
}

export interface UseCreateConnectionReturn {
  create: (credentials: PlatformCredentials) => Promise<PlatformConnection>;
  loading: boolean;
  error: string | null;
}

export function useCreateConnection(): UseCreateConnectionReturn {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const create = useCallback(async (credentials: PlatformCredentials): Promise<PlatformConnection> => {
    setLoading(true);
    setError(null);
    try {
      const connection = await api.post<PlatformConnection>("/api/connections", credentials);
      return connection;
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to create connection";
      setError(msg);
      throw err;
    } finally {
      setLoading(false);
    }
  }, []);

  return { create, loading, error };
}

export interface UseDeleteConnectionReturn {
  remove: (id: string, cascade?: boolean) => Promise<void>;
  loading: boolean;
  error: string | null;
}

export function useDeleteConnection(): UseDeleteConnectionReturn {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const remove = useCallback(async (id: string, cascade?: boolean): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      await api.delete<void>(`/api/connections/${id}?cascade=${cascade !== false}`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to delete connection";
      setError(msg);
      throw err;
    } finally {
      setLoading(false);
    }
  }, []);

  return { remove, loading, error };
}

export interface UseUpdateCredentialsReturn {
  update: (id: string, credentials: Record<string, string>) => Promise<PlatformConnection>;
  loading: boolean;
  error: string | null;
}

export function useUpdateCredentials(): UseUpdateCredentialsReturn {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const update = useCallback(async (id: string, credentials: Record<string, string>): Promise<PlatformConnection> => {
    setLoading(true);
    setError(null);
    try {
      const connection = await api.patch<PlatformConnection>(`/api/connections/${id}/credentials`, { credentials });
      return connection;
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to update credentials";
      setError(msg);
      throw err;
    } finally {
      setLoading(false);
    }
  }, []);

  return { update, loading, error };
}

export interface UseConnectionChannelsReturn {
  channels: AvailableChannel[];
  loading: boolean;
  error: string | null;
  refetch: () => void;
}

export function useConnectionChannels(id: string | null): UseConnectionChannelsReturn {
  const [channels, setChannels] = useState<AvailableChannel[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchChannels = useCallback(() => {
    if (!id) return;
    setLoading(true);
    setError(null);
    api
      .get<AvailableChannel[]>(`/api/connections/${id}/channels`)
      .then(setChannels)
      .catch((err) => {
        setError(err instanceof Error ? err.message : "Failed to load channels");
      })
      .finally(() => setLoading(false));
  }, [id]);

  useEffect(() => {
    fetchChannels();
  }, [fetchChannels]);

  return { channels, loading, error, refetch: fetchChannels };
}

export interface UseUpdateChannelsReturn {
  updateChannels: (selected: string[]) => Promise<void>;
  loading: boolean;
  error: string | null;
}

export function useUpdateChannels(id: string | null): UseUpdateChannelsReturn {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const updateChannels = useCallback(
    async (selected: string[]): Promise<void> => {
      if (!id) return;
      setLoading(true);
      setError(null);
      try {
        await api.put<void>(`/api/connections/${id}/channels`, { selected_channels: selected });
      } catch (err) {
        const msg = err instanceof Error ? err.message : "Failed to update channels";
        setError(msg);
        throw err;
      } finally {
        setLoading(false);
      }
    },
    [id],
  );

  return { updateChannels, loading, error };
}
